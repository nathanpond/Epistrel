"""#777: ``GET /stories/{id}/outbox`` shows on a parked ``imported_unsettled`` row that the
author resolved it (``POST /stories/{id}/reconciliation/{outbox_id}/resolve``): ``resolution``
``{kind, resolved_at, operation_id}``, its ``status`` unchanged. Every other row, and a parked
row nothing resolved, carries ``resolution: null``; a refused resolve sets nothing; a redacted
row keeps it (ids and codes only); and, like the closed listing, it stays set when a truncation
takes the resolving slot out of the active timeline."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ids import new_ulid
from tests.event_support import execute_in_tx, rows
from tests.integration.test_i_10 import _exported, _import, auth_settings_stub
from tests.story_support import READ, WRITE

pytestmark = pytest.mark.integration


async def _outbox(client: Any, story: str) -> dict[str, dict[str, Any]]:
    found = await client.get(
        f"/stories/{story}/outbox", params={"viewpoint": "omniscient"}, headers=READ
    )
    assert found.status_code == 200, found.text
    return {r["outbox_id"]: r for r in found.json()["rows"]}


async def _listed(client: Any, story: str) -> set[str]:
    found = await client.get(
        f"/stories/{story}/reconciliation", params={"viewpoint": "omniscient"}, headers=READ
    )
    assert found.status_code == 200, found.text
    return {e.get("outbox_id") for e in found.json()["listings"]}


async def _resolve(client: Any, story: str, listing: str, kind: str) -> Any:
    return await client.post(
        f"/stories/{story}/reconciliation/{listing}/resolve",
        json={"resolution": kind},
        headers=WRITE,
    )


@pytest.mark.usefixtures("drained_embedding_jobs", "states_delivered")
async def test_resolved_parked_row_shows_its_resolution(
    pg_url: str, engine: AsyncEngine, tmp_path: Any
) -> None:
    from tests import late_settlement_support as ls
    from tests import provider_support as ps
    from tests import staged_support as sp
    from tests import state_support as ss
    from tests.respond_support import respond_client

    async with respond_client(auth_settings_stub(pg_url), engine) as (client, epistrel):
        story, cal, bank, _fake = await sp.sync_story(client, engine, epistrel)
        first, _ = await ls.intent(engine, story, bank, cal, [])
        second, _ = await ls.intent(engine, story, bank, cal, [])
        done, _ = await ls.intent(engine, story, bank, cal, [])
        await ls.answer(engine, epistrel, done)
        await ps.drop_outbox_jobs(engine, story)
        data = await _exported(client, engine, story, tmp_path)
        made = await _import(client, engine, data, metadata={"ignore_warnings": True})
        assert made.ready, made.status
        imported = made.story_id
        parked = await rows(
            engine,
            "SELECT o.id, m.detail FROM external_effect_outbox o JOIN reconciliation_marks m "
            "ON m.outbox_id = o.id WHERE o.story_id = :s AND o.status = 'imported_unsettled'",
            s=imported,
        )
        by_source = {r.detail["source_outbox_id"]: str(r.id) for r in parked}
        assert set(by_source) == {first, second}
        a, b = by_source[first], by_source[second]
        (kept,) = [k for k in await _outbox(client, imported) if k not in (a, b)]

        # Nothing resolved yet: every row, parked or not, carries resolution null.
        before = await _outbox(client, imported)
        assert before[kept]["status"] == "confirmed"
        assert {k: (r["status"], r["resolution"]) for k, r in before.items()} == {
            a: ("imported_unsettled", None),
            b: ("imported_unsettled", None),
            kept: ("confirmed", None),
        }

        # Refusals set nothing: an unknown id, a non-imported row, and States off.
        unknown = await _resolve(client, imported, new_ulid(), "abandoned")
        assert unknown.status_code == 404, unknown.text
        not_parked = await _resolve(client, imported, kept, "abandoned")
        assert not_parked.status_code == 404, not_parked.text
        assert not_parked.json()["error"]["details"]["reason"] == "no_listing"
        await ss.set_level(engine, imported, "off")
        off = await _resolve(client, imported, a, "abandoned")
        assert off.status_code == 409, off.text
        assert off.json()["error"]["code"] == "module_off"
        await ss.set_level(engine, imported, "on")
        assert {r["resolution"] for r in (await _outbox(client, imported)).values()} == {None}

        # The resolve: set on that row only; its status stays imported_unsettled.
        resolved = await _resolve(client, imported, a, "abandoned")
        assert resolved.status_code == 200, resolved.text
        operation_id = resolved.json()["operation_id"]
        after = await _outbox(client, imported)
        assert after[a]["status"] == "imported_unsettled"
        assert after[a]["resolution"]["kind"] == "abandoned"
        assert after[a]["resolution"]["operation_id"] == operation_id
        assert after[a]["resolution"]["resolved_at"]
        assert set(after[a]["resolution"]) == {"kind", "resolved_at", "operation_id"}
        assert (after[b]["resolution"], after[kept]["resolution"]) == (None, None)
        assert a not in await _listed(client, imported)
        assert b in await _listed(client, imported)

        # A rewind past the resolving slot: the listing does not reopen, so the resolution
        # stays (the mark, not the slot's membership, closed it).
        (slot,) = await rows(
            engine,
            "SELECT narrative_turn FROM operations WHERE operation_id = :o",
            o=operation_id,
        )
        rewound = await client.post(
            f"/stories/{imported}/truncate", json={"to_turn": slot.narrative_turn}, headers=WRITE
        )
        assert rewound.status_code == 200, rewound.text
        (gone,) = await rows(
            engine, "SELECT membership FROM operations WHERE operation_id = :o", o=operation_id
        )
        assert str(getattr(gone.membership, "value", gone.membership)) == "discarded"
        assert a not in await _listed(client, imported)
        again = await _resolve(client, imported, a, "applied_elsewhere")
        assert again.status_code == 404, again.text
        rewound_rows = await _outbox(client, imported)
        assert rewound_rows[a]["resolution"] == after[a]["resolution"]
        assert rewound_rows[a]["status"] == "imported_unsettled"

        # The other kind, on the other parked row.
        other = await _resolve(client, imported, b, "applied_elsewhere")
        assert other.status_code == 200, other.text
        both = await _outbox(client, imported)
        assert both[b]["resolution"]["kind"] == "applied_elsewhere"
        assert both[b]["resolution"]["operation_id"] == other.json()["operation_id"]
        assert both[a]["resolution"] == after[a]["resolution"]
        assert both[kept]["resolution"] is None

        # A redacted row keeps its resolution (ids and codes only), never its effect.
        await execute_in_tx(
            engine,
            (
                "UPDATE external_effect_outbox SET effect = '{}', "
                "erased_at = transaction_timestamp() WHERE id = :o",
                {"o": a},
            ),
        )
        redacted = (await _outbox(client, imported))[a]
        assert redacted["redacted"] is True
        assert "effect" not in redacted
        assert redacted["resolution"] == after[a]["resolution"]
        await ps.drop_outbox_jobs(engine, imported)
