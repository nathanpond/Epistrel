"""The outbox's outcome mapping (FR-EXT-15,16,18; #210), without a database.

Only the provider's answer decides ``confirmed`` or ``failed``: the HTTP manifest's
``outcome_map`` over the value at ``outcome_path``, or the in-process ``WriteOutcome.status``. A
timeout (the write template's ``timeout_seconds``), a transport error, a non-2xx answer (whatever
its body says) and an unmapped or missing outcome value are ``unknown``; the dispatcher's
``outcome_status`` turns anything that is not one of the three statuses into ``unknown``.
``effect_of`` builds the port's ``Effect`` with the row id as the idempotency key.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

from epistrel.core.providers import ProviderRow
from epistrel.orchestrator.jobs.handlers.outbox import effect_of, outcome_payload, outcome_status
from epistrel.orchestrator.providers.http_manifest import HttpManifestProvider
from epistrel.orchestrator.providers.port import Binding, Effect, WriteOutcome
from tests import provider_support as ps

HOST = "127.0.0.1:8099"
BASE = f"http://{HOST}"
KEY = "01JOUTBOX0000000000000000A"


def _row(**write: Any) -> ProviderRow:
    spec = ps.http_spec(BASE, "bank", write=ps.write_template(BASE, **write))
    now = datetime.now(UTC)
    return ProviderRow(
        name="bank",
        trust_tier="authoritative",
        transport="http",
        manifest=spec["manifest"],
        write_capable=True,
        confirmation="asynchronous",
        dedupes_dispatch=True,
        returns_resulting_values=True,
        unavailability="last_known",
        freshness_minutes=None,
        confirm_wait_max_seconds=10,
        config_revision=1,
        created_at=now,
        updated_at=now,
    )


def _write(handler: Any, **write: Any) -> WriteOutcome:
    provider = HttpManifestProvider(_row(**write), [HOST], transport=httpx2.MockTransport(handler))
    effect = Effect(value="gold", idempotency_key=KEY, delta=Decimal(-50))
    binding = Binding("s-1", "bank", {"player": "p-1"})
    return asyncio.run(provider.write(effect, binding))


def _answer(status: int, body: Any) -> Any:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["Idempotency-Key"] == KEY
        return httpx2.Response(status, content=json.dumps(body).encode())

    return handler


def test_a_mapped_answer_decides() -> None:
    confirmed = _write(_answer(200, {"status": "ok", "values": {"gold": 250}}))
    assert (confirmed.status, dict(confirmed.resulting_values or {})) == (
        "confirmed",
        {"gold": 250},
    )
    assert outcome_status(confirmed) == "confirmed"
    assert outcome_payload(confirmed) == {"resulting_values": {"gold": 250}}
    failed = _write(_answer(200, {"status": "declined"}))
    assert outcome_status(failed) == "failed"
    assert outcome_payload(failed) == {}


def test_an_unmapped_or_missing_value_is_unknown() -> None:
    assert _write(_answer(200, {"status": "pending"})).status == "unknown"
    assert _write(_answer(200, {"other": "ok"})).status == "unknown"
    assert _write(_answer(200, "not an object")).status == "unknown"


@pytest.mark.parametrize("status", [500, 503, 404, 302, 409])
def test_a_non_2xx_answer_is_unknown_whatever_its_body(status: int) -> None:
    found = _write(_answer(status, {"status": "ok"}))
    assert found.status == "unknown"
    assert found.raw_hash


def test_a_transport_error_is_unknown() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    assert _write(handler).status == "unknown"


def test_a_timeout_is_unknown() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    assert _write(handler, timeout_seconds=1).status == "unknown"


def test_outcome_status_admits_only_the_three_statuses() -> None:
    assert outcome_status(WriteOutcome(status="failed")) == "failed"
    assert outcome_status(WriteOutcome(status="unknown")) == "unknown"
    assert outcome_status(SimpleNamespace(status="applied")) == "unknown"
    assert outcome_status(None) == "unknown"
    assert outcome_status("confirmed") == "unknown"


def test_effect_of_keys_the_send_by_the_row_id() -> None:
    row = SimpleNamespace(
        id=KEY,
        value_name="coins",
        scope_key="c-1",
        effect={"kind": "change", "state": "gold", "delta": -50, "reason": "a sword"},
    )
    assert effect_of(row) == Effect(
        value="coins", idempotency_key=KEY, delta=Decimal(-50), scope_key="c-1"
    )
    row.effect = {"kind": "change", "state": "mood", "value": "calm", "reason": "rested"}
    assert effect_of(row).new_value == "calm"
    assert effect_of(row).delta is None
