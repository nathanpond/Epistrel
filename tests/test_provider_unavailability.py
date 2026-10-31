"""Unit tests of #206's unavailability rules (FR-EXT-7,11): what an ``Unknown`` pin settles and
blocks, the report entry, and the freshness default. S-EXT-UNAVAILABLE plays them end to end
(``tests/scenario/test_s_ext_unavailable.py``)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from epistrel.core.enums import ExternalValueSource
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.core.states import external
from epistrel.core.states import verify as sv
from epistrel.core.states.external import ExternalPin
from epistrel.core.verify import rules
from epistrel.orchestrator.providers import unavailability
from epistrel.orchestrator.structured import pipeline
from epistrel.orchestrator.structured import steps as render_steps
from epistrel.orchestrator.verify.gate import NEVER_ESCALATES

STATE, CAL, BOBBY = new_ulid(), new_ulid(), new_ulid()


class _Pins:
    """A turn's pins as Core reads them: Cal's gold pinned with ``source``."""

    def __init__(self, source: ExternalValueSource) -> None:
        self.source = source

    @property
    def turn(self) -> int:
        return 3

    def pinned(self, state_id: str, scope_key: str | None, through: Position | None) -> Any:
        del through
        if (state_id, scope_key) != (STATE, CAL):
            return None
        value = None if self.source is ExternalValueSource.UNKNOWN else 300
        return ExternalPin(STATE, CAL, value, Position(3), source=self.source)  # type: ignore[arg-type]


def test_unknown_pin_settles_nothing() -> None:
    """An ``Unknown`` value is ``uncertain(provider_unknown)`` either way (FR-EXT-11), and the
    reason never escalates to the entailment model (no model judges a value no one has)."""
    claim = sv.proposition(STATE, CAL, sv.number_literal(500))
    unknown = rules.OwnedValue(
        proposition=sv.proposition(STATE, CAL, None),
        ref=f"state:{STATE}",
        kind="number",
        unsettled=rules.VerdictReason.PROVIDER_UNKNOWN,
    )
    found = sv.verdict(claim, [unknown])
    assert (found.kind, found.reason) == (
        rules.VerdictKind.UNCERTAIN,
        rules.VerdictReason.PROVIDER_UNKNOWN,
    )
    assert found.confidence == 0.0
    assert rules.VerdictReason.PROVIDER_UNKNOWN in NEVER_ESCALATES


def test_unknown_now() -> None:
    """The observation writer's test: a ``state_value`` of a value pinned ``Unknown`` this
    turn; a live or ``last_known`` pin, another value, and no turn at all are not."""
    claim = sv.proposition(STATE, CAL, sv.number_literal(500))
    assert external.unknown_now(claim) is False  # outside a turn
    with external.pinning(_Pins(ExternalValueSource.UNKNOWN)):
        assert external.unknown_now(claim) is True
        assert external.unknown_now(sv.proposition(STATE, BOBBY, None)) is (False)
    for source in (ExternalValueSource.READ, ExternalValueSource.LAST_KNOWN):
        with external.pinning(_Pins(source)):
            assert external.unknown_now(claim) is False


async def test_observe_step_of_unknown_is_rejected() -> None:
    """An ``observe`` step (#168) of a value pinned ``Unknown`` is rejected
    (``provider_unknown``): no character newly learns it this turn."""
    claim = sv.proposition(STATE, CAL, sv.number_literal(500))

    async def deterministic(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return rules.uncertain(rules.VerdictReason.PROVIDER_UNKNOWN), None

    verifier = SimpleNamespace(
        config=SimpleNamespace(on=True),
        source=SimpleNamespace(deterministic=deterministic),
        beliefs=None,
        canon_requests=list,
    )
    read = SimpleNamespace(character=SimpleNamespace(id=BOBBY), turn=3)
    stepper = pipeline._stepper(
        None,  # type: ignore[arg-type]  # not read for a state_value
        new_ulid(),
        read,  # type: ignore[arg-type]
        verifier,  # type: ignore[arg-type]
        [render_steps.Step(1, render_steps.OBSERVE, proposition=claim)],
        first_beat=1,
        operation_id=new_ulid(),
        read_set=None,  # type: ignore[arg-type]
    )
    assert stepper is not None
    found = await stepper.run_all()
    assert (found[1].verified, found[1].reason) == (False, render_steps.PROVIDER_UNKNOWN)


def test_policy_report_and_freshness() -> None:
    """The report entry's shape, and ``last_known``'s 60-minute default window."""
    applied = unavailability.Applied("ledger", "gold", "last_known", 12, "timeout")
    assert applied.report() == {
        "provider": "ledger",
        "value": "gold",
        "policy": "last_known",
        "age_minutes": 12,
        "reason": "timeout",
    }
    row = SimpleNamespace(freshness_minutes=None)
    assert unavailability.freshness(row) == 60  # type: ignore[arg-type]
    assert unavailability.freshness(SimpleNamespace(freshness_minutes=5)) == 5  # type: ignore[arg-type]
