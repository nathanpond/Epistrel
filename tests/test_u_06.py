"""U-06 (M4): the mutation primitives (docs/04 §3, FR-GUARD-4; #145). Every U-06 part lives in
this file (traceability counts a test ID per milestone in one file).

``test_u_06`` runs the three primitives over the REST ``/admin`` routes against PostgreSQL, on
#133's world (Cal the player, Bobby, Ann) on the seeded ``Structured (M4)`` profile:

- **quarantine** excludes a memory from retrieval: the debugging read returns it before and
  not after (the complement), shows it again, flagged ``quarantined``, only with
  ``include_quarantined``, and finds it again once the quarantine is reversed;
- **sanitize** substitutes the surrogate in the context: the assembled context holds the
  surrogate and not the original span, which survives only in the ``sanitize_original`` audit
  event; a span the text does not hold is 422 ``span_not_found`` with nothing written;
- **canonize** promotes with provenance: an author ``commit_fact`` writes a fact whose
  ``provenance`` and ``authority`` are ``author`` and an author memory retrieval finds.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.routes.admin import ADMIN_PREFIX
from tests import canon_support as c
from tests import consolidation_support as cs
from tests.auth_support import auth_settings, bearer
from tests.event_support import rows, scalar
from tests.memory_support import turn
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

WRITE = {**bearer(), "X-Epistrel-Principal": "p-author"}
OMNISCIENT = {"viewpoint": "omniscient"}


def _found(body: dict[str, Any]) -> dict[str, str]:
    """Memory id → text of every memory a retrieval returned."""
    return {
        m["id"]: m["text"] for m in (*body["episodes"], *body["assessments"], *body["memories"])
    }


def _context(body: dict[str, Any]) -> str:
    return "\n".join(item["text"] for item in body["context"]["items"])


@pytest.mark.part("M4", "FR-GUARD-4")
async def test_u_06(pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None) -> None:
    """Quarantine excludes from retrieval; sanitize substitutes the surrogate; canonize
    promotes with provenance (see the module docstring)."""
    w = await cs.story(engine)
    story, bobby = w.story_id, w.characters["Bobby"]
    _, (planted,) = await turn(
        engine, story, actor=bobby, say="The vault code is under the lantern.", reply=None
    )
    _, (hidden,) = await turn(
        engine, story, actor=bobby, say="Ann hid the brass key under the doormat.", reply=None
    )
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, _):

        async def retrieve(q: str, **extra: Any) -> dict[str, Any]:
            found = await client.get(
                f"/stories/{story}/retrieve",
                params={"q": q, **OMNISCIENT, **extra},
                headers=bearer(),
            )
            assert found.status_code == 200, found.text
            body: dict[str, Any] = found.json()
            return body

        # Quarantine: retrieved before, not after (the complement), and back on release.
        assert planted.id in _found(await retrieve("vault code lantern"))
        before = int(
            await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story)
        )
        quarantined = await client.post(
            f"{ADMIN_PREFIX}/stories/{story}/memories/{planted.id}/quarantine",
            json={"reason": "A planted line."},
            headers=WRITE,
        )
        assert quarantined.status_code == 200, quarantined.text
        written = quarantined.json()
        assert written["memory"]["quarantined"] is True
        assert written["memory"]["quarantine_reason"] == "A planted line."
        assert written["memory"]["quarantined_by"] == "p-author"
        assert written["timeline_revision"] == before + 1
        assert planted.id not in _found(await retrieve("vault code lantern"))
        debug = await retrieve("vault code lantern", include_quarantined="true")
        flagged = [m for m in debug["memories"] if m["id"] == planted.id]
        assert len(flagged) == 1
        assert flagged[0]["quarantined"] is True
        again = await client.post(
            f"{ADMIN_PREFIX}/stories/{story}/memories/{planted.id}/quarantine",
            json={"reason": "A planted line."},
            headers=WRITE,
        )
        assert again.status_code == 200
        assert again.json()["operation_id"] is None  # already quarantined: a no-op
        released = await client.delete(
            f"{ADMIN_PREFIX}/stories/{story}/memories/{planted.id}/quarantine", headers=WRITE
        )
        assert released.status_code == 200, released.text
        assert released.json()["memory"]["quarantined"] is False
        assert planted.id in _found(await retrieve("vault code lantern"))

        # Sanitize: the surrogate is in the context, the original span is not.
        assert "under the doormat" in _context(await retrieve("brass key Ann hid"))
        missing = await client.post(
            f"{ADMIN_PREFIX}/stories/{story}/memories/{hidden.id}/sanitize",
            json={"span": "under the flowerpot", "surrogate": "somewhere safe"},
            headers=WRITE,
        )
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "span_not_found"
        sanitized = await client.post(
            f"{ADMIN_PREFIX}/stories/{story}/memories/{hidden.id}/sanitize",
            json={"span": "under the doormat", "surrogate": "somewhere safe"},
            headers=WRITE,
        )
        assert sanitized.status_code == 200, sanitized.text
        assert (
            sanitized.json()["memory"]["text"]
            == "Bobby says: Ann hid the brass key somewhere safe."
        )
        context = _context(await retrieve("brass key Ann hid"))
        assert "somewhere safe" in context
        assert "under the doormat" not in context
        (audit,) = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'sanitize_original'",
            s=story,
        )
        assert audit.payload["span"] == "under the doormat"
        (narrative,) = await rows(
            engine,
            "SELECT category::text AS category, payload FROM events "
            "WHERE story_id = :s AND type = 'sanitized'",
            s=story,
        )
        assert narrative.category == "narrative_state"
        assert narrative.payload == {
            "memory_id": hidden.id,
            "surrogate": "somewhere safe",
            "occurrences": 1,
        }
        assert "under the doormat" not in str(narrative.payload)

        # Canonize: an author fact through the admin route, with its provenance.
        proposition = c.at(w.people["Bobby"], w.places["school"], 0, None).model_dump(mode="json")
        committed = await client.post(
            f"{ADMIN_PREFIX}/stories/{story}/facts",
            json={"proposition": proposition, "locked": True},
            headers=WRITE,
        )
        assert committed.status_code == 201, committed.text
        fact = committed.json()["fact"]
        assert (fact["provenance"], fact["authority"], fact["author_locked"]) == (
            "author",
            "author",
            True,
        )
        (row,) = await rows(
            engine,
            "SELECT provenance::text AS provenance, authority::text AS authority FROM facts "
            "WHERE id = :f",
            f=fact["id"],
        )
        assert (row.provenance, row.authority) == ("author", "author")
        memory_id = committed.json()["memory_id"]
        assert memory_id is not None
        (memory,) = await rows(
            engine,
            "SELECT provenance::text AS provenance, trust_tier::text AS tier, text FROM memories "
            "WHERE id = :m",
            m=memory_id,
        )
        assert (memory.provenance, memory.tier) == ("author", "author")
        assert memory_id in _found(await retrieve("Bobby school"))
        listed = await client.get(
            f"{ADMIN_PREFIX}/stories/{story}/facts", params=OMNISCIENT, headers=bearer()
        )
        assert listed.status_code == 200, listed.text
        assert fact["id"] in {f["id"] for f in listed.json()["facts"]}
