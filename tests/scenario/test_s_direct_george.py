"""S-DIRECT-GEORGE (docs/04 §7; FR-COMMIT-12,14, FR-DIRECT-1; #241): the author directs who
knocked, and the engine still makes sure the story can live with it.

S-KNOCK's house (``tests.scenario.test_s_knock``): turn 1 narrates a knock and leaves "who
knocked" an open thunk.

- **George** (unmodeled, nothing recorded about him): turn 2 is regenerated with the directive
  "The knocker is George, her ex." The renders carry "Author decision: George knocked_at the
  door" instead of the deferral bias, the reconciliation runs (a spy counts it), there is no
  conflict, and the regenerated turn commits George as the knocker with ``authority = author``:
  zero amendments, zero rewrite sessions, one ``directives`` row (``directed_regeneration``),
  and the regenerate operation names the variant it replaced (``regenerated_variant``).
- **Bobby**, whose later sincere line ("I never knocked on that door") contradicts having
  knocked: the author's choice stands but is held as a ``directed_resolution`` amendment, 202
  with the session (its conflicts list Bobby's line); nothing commits before apply (the old
  variant active, the revision unchanged); cancelled, the story is as it was. Under
  ``best_effort`` + ``apply_directly`` the amendment (Bobby's line repaired) and the
  regenerated variant (Bobby the knocker, never re-steered) commit in the one call.
- A directive on a Lite story is a nudge for that regeneration only (#244); a Lite regenerate
  without one works as before.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.commit.reconcile import Conflict
from epistrel.orchestrator.commit import directed
from epistrel.orchestrator.structured.prompt import RESOLUTION_STEERING
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, played, respond_client
from tests.scenario.test_s_knock import _house, _knock, _knockers
from tests.story_support import WRITE, create, story_body
from tests.structured_turn_support import beat, claim, render

pytestmark = pytest.mark.integration

GEORGE = "The knocker is George, her ex."
BOBBY = "The knocker is Bobby."
NEVER = "I was out back all morning. I never knocked on that door."


def _parsed(name: str) -> str:
    return json.dumps(
        {
            "classification": "durable",
            "binding": [{"entity": name, "about": "who knocked"}],
            "steer_text": f"The knocker is {name}.",
        }
    )


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.fixture
def reconciled(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """A spy on the reconciliation a directed regeneration always runs."""
    calls: list[Any] = []
    real = directed.reconcile  # type: ignore[attr-defined]

    async def spy(*args: Any, **kwargs: Any) -> Any:
        found = await real(*args, **kwargs)
        calls.append(found)
        return found

    monkeypatch.setattr(directed, "reconcile", spy)
    return calls


def _regenerate(client: Any, story: str, **fields: Any) -> Any:
    return client.post(f"/stories/{story}/regenerate", json=fields, headers=WRITE)


def _prompts(stub: Any) -> str:
    return "\n".join(m.content for c in stub.calls if c.purpose == "generation" for m in c.messages)


@pytest.mark.part("M7", "FR-COMMIT-12,14; FR-DIRECT-1")
async def test_s_direct_george(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    reconciled: list[Any],
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _house(client, engine)
        await _knock(client, engine, stub, story, ids)
        stub.reset()
        stub.script("generation", [render(beat("narration", "The house is quiet."))])
        await played(client, story, body(ids["Wren"], "Who's there?"))
        assert await _knockers(engine, story, ids) == []

        stub.reset()
        stub.when("", _parsed("George"), purpose="directive_parse")
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "do",
                        "Jenni opens the door: it is George, her ex, his hand raised to knock.",
                        propositions=[claim("George", "knocked_at", "the door")],
                    )
                )
            ],
        )
        regenerated = await _regenerate(client, story, directive={"text": GEORGE})
        assert regenerated.status_code == 200, regenerated.text
        assert len(reconciled) == 1
        assert not isinstance(reconciled[0], Conflict)
        prompts = _prompts(stub)
        assert "Author decision: George" in prompts
        assert RESOLUTION_STEERING not in prompts
        (knocker,) = await _knockers(engine, story, ids)
        (fact,) = await rows(
            engine,
            "SELECT f.authority, e.canonical_name FROM facts f JOIN entities e "
            "ON e.id = f.subject_id WHERE f.id = :f",
            f=knocker.id,
        )
        assert (fact.authority, fact.canonical_name) == ("author", "George")
        assert await rs.count(engine, "amendments", story) == 0
        assert await rs.count(engine, "rewrite_sessions", story) == 0
        (row,) = await rows(
            engine, "SELECT disposition FROM directives WHERE story_id = :s", s=story
        )
        assert row.disposition == "directed_regeneration"
        (report,) = await rows(
            engine,
            "SELECT turn_report -> 'regenerated_variant' AS replaced FROM operations "
            "WHERE story_id = :s AND kind = 'turn' AND membership = 'active' "
            "AND narrative_turn = 2",
            s=story,
        )
        assert report.replaced is not None


@pytest.mark.part("M7", "FR-COMMIT-12,14; FR-DIRECT-1")
async def test_s_direct_george__bobby(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    reconciled: list[Any],
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _house(client, engine)
        await _knock(client, engine, stub, story, ids)
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        NEVER,
                        intent="sincere",
                        propositions=[claim("I", "knocked_at", "the door", negated=True)],
                    )
                )
            ],
        )
        await played(
            client,
            story,
            body(ids["Wren"], "Bobby, did you knock?", render_character_ids=[ids["Bobby"]]),
        )
        stub.reset()
        stub.script("generation", [render(beat("narration", "Nobody answers."))])
        await played(client, story, body(ids["Wren"], "Hello?"))
        revision = await rs.revision(engine, story)
        before = await _knockers(engine, story, ids)

        stub.reset()
        stub.when("", _parsed("Bobby"), purpose="directive_parse")
        held = await _regenerate(client, story, directive={"text": BOBBY})
        assert held.status_code == 202, held.text
        answer = held.json()
        assert answer["disposition"] == "awaiting_rewrite"
        session = answer["rewrite_session"]
        assert session["kind"] == "directed_resolution"
        assert isinstance(reconciled[-1], Conflict)
        conflicts = {c["record"]["id"] for c in session["preview"]["blast_radius"]["irreducible"]}
        # What canon recorded from Bobby's line ("I never knocked"): his negated knock.
        recorded = await rows(
            engine,
            "SELECT p.subject_id, p.polarity FROM facts f JOIN propositions p "
            "ON p.id = f.proposition_id WHERE f.id = ANY(:i)",
            i=sorted(conflicts),
        )
        assert (ids["Bobby"], "negated") in {(r.subject_id, str(r.polarity)) for r in recorded}
        assert await rs.revision(engine, story) == revision
        assert await rs.count(engine, "amendments", story) == 0
        assert await _knockers(engine, story, ids) == before
        assert [c for c in stub.calls if c.purpose == "generation"] == []
        cancelled = await client.post(
            f"/stories/{story}/rewrites/{session['id']}/cancel",
            params=rs.OMNISCIENT,
            headers=WRITE,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert await rs.revision(engine, story) == revision
        assert await _knockers(engine, story, ids) == before

        # best_effort + apply_directly: the amendment and the regenerated variant together.
        stub.reset()
        stub.when("", _parsed("Bobby"), purpose="directive_parse")
        stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "do",
                        "Jenni opens the door: Bobby stands there, knuckles still raised.",
                        propositions=[claim("Bobby", "knocked_at", "the door")],
                    )
                )
            ],
        )
        applied = await _regenerate(
            client,
            story,
            directive={"text": BOBBY},
            resolution_mode="best_effort",
            preview="apply_directly",
        )
        assert applied.status_code == 200, applied.text
        (amendment,) = await rows(
            engine, "SELECT kind, status FROM amendments WHERE story_id = :s", s=story
        )
        assert (amendment.kind, amendment.status) == ("directed_resolution", "active")
        knockers = await _knockers(engine, story, ids)
        assert ids["Bobby"] in {
            k.subject_id for k in knockers if k.id not in {b.id for b in before}
        }
        assert "Author decision: Bobby" in _prompts(stub)


async def test_regenerate_without_directive_unchanged(pg_url: str, engine: AsyncEngine) -> None:
    """#244 (FR-LITE-8): on a Lite story a directive is a nudge for that regeneration only (a
    ``directed_regeneration`` row, the text in the regeneration's prompt and not the next
    turn's); without a directive the Lite regenerate works as before."""
    from tests.respond_support import FIRST, respond

    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        story = (await create(client, story_body("Wren", "Ada")))["story_id"]
        cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
        wren = next(str(c.id) for c in cast if c.name == "Wren")
        first = await respond(client, story, body(wren, FIRST))
        assert first.status_code == 200, first.text
        epistrel.models.stub.calls.clear()
        directed_ = await _regenerate(client, story, directive={"text": GEORGE})
        assert directed_.status_code == 200, directed_.text
        assert GEORGE in _prompts(epistrel.models.stub)
        (row,) = await rows(
            engine, "SELECT disposition FROM directives WHERE story_id = :s", s=story
        )
        assert row.disposition == "directed_regeneration"
        again = await _regenerate(client, story)
        assert again.status_code == 200, again.text


def test_no_author_directed_stub() -> None:
    """#241: #154's ``author_directed`` stub is gone (the directed path is
    ``orchestrator.commit.directed``)."""
    from pathlib import Path

    import epistrel.core.commit.reconcile as module

    assert "NotImplementedError" not in Path(module.__file__).read_text()
    assert not hasattr(module, "author_directed")
