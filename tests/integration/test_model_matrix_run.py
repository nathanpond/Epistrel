"""The model matrix tool end to end against a local OpenAI-compatible server (#54): protocol,
capability per purpose, E-06, and timed Lite turns for both rows, then the files it writes.
On the stub adapter, scripted deadline faults show up in the call breakdown (#303).

The real measurement runs against LM Studio on the owner's machine (``test_p_08``, marked
``live_model``); this run proves the tool's plumbing in CI with a 3-turn row."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.context import RequestContext
from epistrel.core.profiles.story import ProfileOverridesPatch
from epistrel.engine import Engine
from epistrel.orchestrator.models.stub import FaultKind, StubAdapter
from epistrel.tools import model_matrix as mm
from tests.auth_support import auth_settings
from tests.model_support import config_key, mock_openai, restored_model_config

DEADLINE_S = 2.0
"""The minors story's turn deadline in the deadline run's hanging turns (its override of
``budgets.deadline_ms``, #113): short, so a hung call ends soon. Every other turn runs under the
profile's own deadline, so only the scripted hang can reach a deadline (#411)."""
PASSED = '{"violation": false, "confidence": 1.0}'
FLAGGED = '{"violation": true, "confidence": 0.9}'
MEASURED = 5
HANGS: dict[int, tuple[str, int]] = {2: ("generation", 2), 4: ("guardrail_check", 3)}
"""Measured turn -> the call of the turn that hangs, after the output check flagged the draft:
turn 2's regeneration (its 2nd ``generation``), turn 4's re-check (its 3rd ``guardrail_check``)."""
STUB_TARGET = mm.Target(base_url="http://stub.invalid/v1", model="stub-model", api_key="k")


def hang_on(purpose: str, nth: int) -> Callable[[str], FaultKind | None]:
    """A stub fault hook: the ``nth`` call of ``purpose`` from now on never answers."""
    seen = 0

    def hook(called: str) -> FaultKind | None:
        nonlocal seen
        if called != purpose:
            return None
        seen += 1
        return "hang" if seen == nth else None

    return hook


@dataclass
class DeadlineDriver:
    """``RespondDriver`` with stub fault injection in the minors row: in the measured turns of
    ``HANGS`` the story runs under a ``DEADLINE_S`` turn deadline, the output check flags the
    draft, the output gate regenerates, and one call then hangs until the deadline cuts it.
    The other turns run under the profile's deadline, however slow the runner (#411)."""

    stub: StubAdapter
    inner: mm.RespondDriver = field(default_factory=mm.RespondDriver)
    name: str = mm.TURN_PATH
    faulty: bool = False
    played: int = -1
    """Turns played so far, the warm-up turn being 0."""
    deadlines: list[int | None] = field(default_factory=list)
    """The minors row's ``deadline_ms`` override in force at each turn (``None``: none)."""
    engine: Engine | None = None
    slow_s: float = 0.0
    """Stub latency per call in the minors row's other turns: a slow runner (#411)."""

    async def prepare(self, engine: Engine, policies: mm.PolicySet) -> None:
        await self.inner.prepare(engine, policies)
        self.engine = engine
        self.faulty = policies == "minors"

    async def _deadline(self, deadline_ms: int | None) -> None:
        assert self.engine is not None
        await self.engine.set_profile_overrides(
            RequestContext(principal=mm.PRINCIPAL),
            self.inner.story_id,
            ProfileOverridesPatch(
                settings={"budgets": {"deadline_ms": deadline_ms}}, ignore_warnings=True
            ),
        )

    async def turn(self, said: str) -> mm.TurnCalls:
        self.played += 1
        hang = HANGS.get(self.played) if self.faulty else None
        if self.faulty:
            self.deadlines.append(None if hang is None else int(DEADLINE_S * 1000))
        if hang is not None:
            await self._deadline(int(DEADLINE_S * 1000))
            self.stub.script("guardrail_check", [PASSED, FLAGGED])
            self.stub.fault_hook = hang_on(*hang)
        elif self.faulty:
            self.stub.latency_s = self.slow_s
        try:
            return await self.inner.turn(said)
        finally:
            self.stub.fault_hook = None
            self.stub.latency_s = 0.0
            if hang is not None:
                await self._deadline(None)


async def test_matrix_run_against_a_local_server(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """Two rows of ``respond`` turns (#57): one ``generation`` call per turn with policies off,
    and three with the minors row, where the seeded minors policy applies to the cast's
    12-year-old and checks the input and the output (#68).

    Complement asserted: the endpoint saw ``GET /v1/models`` (the protocol probe) as well as
    completions, so protocol was measured rather than assumed."""
    settings = auth_settings(pg_url, model_adapter="openai", config_key=config_key())
    with mock_openai(models=("local-model",)) as mock:
        async with restored_model_config(engine):
            epistrel = Engine(settings, db=engine)
            try:
                target = mm.Target(
                    base_url=f"{mock.root}/v1",
                    model="local-model",
                    api_key="lm-studio",
                    hardware="test runner",
                    server_version="mock",
                )
                rows = await mm.run_matrix(epistrel, target, turns=3)
            finally:
                await epistrel.models.aclose()
    assert rows is not None
    assert [r.policies for r in rows] == ["off", "minors"]
    assert any(r.path == "/v1/models" for r in mock.requests)
    for r in rows:
        assert r.protocol
        assert r.turns == 3
        assert len(r.latencies_ms) == 3
        assert r.p50_ms is not None
        assert r.p95_ms is not None
        assert r.p50_ms <= r.p95_ms
        assert all(cell["capable"] for cell in r.capability.values())
        assert r.e06["passed"]
        assert r.turn_path == mm.TURN_PATH
    assert rows[0].calls_per_turn == {"generation": 1.0}
    assert rows[1].calls_per_turn == {"generation": 1.0, "guardrail_check": 2.0}
    checks = [
        r.body["messages"][0]["content"]
        for r in mock.requests
        if r.path.endswith("/chat/completions")
        and "## Subjects" in r.body["messages"][0]["content"]
    ]
    # The seeded age-conditioned policy, with the cast's 12-year-old as a subject.
    assert checks
    assert all("age 12" in system for system in checks)

    markdown, raw = tmp_path / "model-matrix.md", tmp_path / "model-matrix.json"
    mm.write_matrix(mm.merge(mm.load_rows(raw), rows), markdown, raw)
    text = markdown.read_text("utf-8")
    assert "| local: local-model | policies off | ✓ |" in text
    assert "| local: local-model | minors policy on | ✓ |" in text


async def stub_run(
    pg_url: str, engine: AsyncEngine, driver: Callable[[Engine], mm.TurnDriver]
) -> list[mm.Row]:
    """``run_matrix`` on the stub adapter with ``MEASURED`` turns per row."""
    settings = auth_settings(pg_url, model_adapter="stub", config_key=config_key())
    async with restored_model_config(engine):
        epistrel = Engine(settings, db=engine)
        try:
            rows = await mm.run_matrix(
                epistrel, STUB_TARGET, turns=MEASURED, driver_factory=lambda: driver(epistrel)
            )
        finally:
            await epistrel.models.aclose()
    assert rows is not None
    return rows


async def test_matrix_breakdown_of_scripted_deadline_turns(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """#303: the issue's live run, scripted with stub fault injection. In the minors row
    (a 2 s turn deadline in turns 2 and 4 only, #411), measured turn 2's output check flags the
    draft and the
    regeneration hangs until the deadline, and turn 4's regeneration answers but its re-check
    hangs. Both turns release the safe fallback with cause ``deadline``. The mean folds all of
    this into ``generation 1.4, guardrail_check 2.2``. The breakdown reports instead: designed
    ``generation 1, guardrail_check 2`` per turn; extra calls ``generation 2,
    guardrail_check 1``; one ``timeout`` of each purpose; turns 2 and 4 as the turns that hit a
    deadline or error (they are the turns that took the deadline); and two ``deadline``
    fallbacks. The md and json written from the rows carry the same breakdown.

    Complement asserted: the policies-off row of the same run had no fault and reports the
    design only: no extra or failed call, no faulted turn, a whole number of calls per turn."""
    drivers: list[DeadlineDriver] = []

    def driver(epistrel: Engine) -> DeadlineDriver:
        drivers.append(DeadlineDriver(epistrel.models.stub))
        return drivers[-1]

    rows = await stub_run(pg_url, engine, driver)
    off, minors = rows
    # #411: only the hanging turns run under the short deadline (warm-up, then turns 1-5).
    short = int(DEADLINE_S * 1000)
    assert [d.deadlines for d in drivers if d.faulty] == [[None, None, short, None, short, None]]
    assert minors.policies == "minors"
    assert minors.calls_per_turn == {"generation": 1.4, "guardrail_check": 2.2}
    assert minors.designed_calls_per_turn == {"generation": 1.0, "guardrail_check": 2.0}
    assert minors.extra_calls == {"generation": 2, "guardrail_check": 1}
    assert minors.failed_calls == {"generation": {"timeout": 1}, "guardrail_check": {"timeout": 1}}
    assert minors.faulted_turns == [2, 4]
    assert minors.fallbacks == {"deadline": 2}
    slow = [minors.latencies_ms[n - 1] for n in minors.faulted_turns]
    assert min(slow) >= DEADLINE_S * 1000
    others = [ms for n, ms in enumerate(minors.latencies_ms, 1) if n not in minors.faulted_turns]

    assert off.calls_per_turn == off.designed_calls_per_turn == {"generation": 1.0}
    assert (off.extra_calls, off.failed_calls, off.faulted_turns, off.fallbacks) == ({}, {}, [], {})

    markdown, raw = tmp_path / "model-matrix.md", tmp_path / "model-matrix.json"
    mm.write_matrix(mm.merge([], rows), markdown, raw)
    text = markdown.read_text("utf-8")
    p50 = mm.nearest_rank(others, 50) / 1000
    p95 = mm.nearest_rank(others, 95) / 1000
    assert (
        "| local: stub-model | minors policy on | 3 (generation 1, guardrail_check 2) "
        "| 3 (generation 2, guardrail_check 1) "
        "| 2 (generation timeout 1, guardrail_check timeout 1) "
        f"| 2 of {MEASURED} (turns 2, 4) | {p50:.2f} s / {p95:.2f} s | 2 (deadline 2) |"
    ) in text
    written = {r["policies"]: r for r in json.loads(raw.read_text("utf-8"))["rows"]}
    assert written["minors"]["extra_calls"] == {"generation": 2, "guardrail_check": 1}
    assert written["minors"]["faulted_turns"] == [2, 4]
    assert written["off"]["extra_calls"] == {}


async def test_a_slow_runner_hits_only_the_scripted_deadlines(
    pg_url: str, engine: AsyncEngine
) -> None:
    """#411: the deadline run on a slow runner. Every call of the minors row's unfaulted turns
    takes 0.8 s (three calls: 2.4 s, past ``DEADLINE_S``), yet only the scripted hangs hit a
    deadline: turns 2 and 4 are the faulted turns, with two ``deadline`` fallbacks and one
    ``timeout`` of each purpose, and the unfaulted turns answered and took the stub's
    latency."""
    slow = 0.8
    rows = await stub_run(
        pg_url, engine, lambda epistrel: DeadlineDriver(epistrel.models.stub, slow_s=slow)
    )
    minors = rows[1]
    assert minors.faulted_turns == [2, 4]
    assert minors.fallbacks == {"deadline": 2}
    assert minors.failed_calls == {"generation": {"timeout": 1}, "guardrail_check": {"timeout": 1}}
    others = [ms for n, ms in enumerate(minors.latencies_ms, 1) if n not in minors.faulted_turns]
    assert min(others) >= 3 * slow * 1000 > DEADLINE_S * 1000


async def test_fault_free_run_reports_the_design_only(pg_url: str, engine: AsyncEngine) -> None:
    """#303's complement: a stub run with no fault reports, for both rows, no extra calls, no
    failed call, no turn that hit a deadline or error and no fallback, and calls per turn that
    are whole numbers equal to the designed calls (one ``generation``; plus the minors
    policy's input and output checks)."""
    rows = await stub_run(pg_url, engine, lambda _: mm.RespondDriver())
    designs = {"off": {"generation": 1.0}, "minors": {"generation": 1.0, "guardrail_check": 2.0}}
    for r in rows:
        assert r.extra_calls == {}
        assert r.failed_calls == {}
        assert r.faulted_turns == []
        assert r.fallbacks == {}
        assert r.calls_per_turn == r.designed_calls_per_turn == designs[r.policies]
        assert all(float(count).is_integer() for count in r.calls_per_turn.values())


async def test_guarded_reply_driver_reports_its_design(pg_url: str, engine: AsyncEngine) -> None:
    """The guarded reply (#53), the turn path of the rows measured before #57, reports its
    design too: one ``generation`` call plus the in-memory minors policy's input and output
    checks. On the stub, with no fault, that is every call it makes."""
    settings = auth_settings(pg_url, model_adapter="stub", config_key=config_key())
    epistrel = Engine(settings, db=engine)
    try:
        _, turns = await mm.measure_turns(epistrel, mm.GuardedReplyDriver(), "minors", 2)
    finally:
        await epistrel.models.aclose()
    breakdown = mm.call_breakdown(turns)
    design = {"generation": 1.0, "guardrail_check": 2.0}
    assert mm.calls_per_turn(turns) == breakdown.designed_calls_per_turn == design
    assert (breakdown.extra_calls, breakdown.faulted_turns) == ({}, [])


async def test_unreachable_endpoint_writes_nothing(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """Complement asserted: an unreachable endpoint exits 1 before any turn or file write, while
    a reachable one (the test above) writes both files."""
    with mock_openai() as mock:
        closed = mock.root
    args = argparse.Namespace(
        base_url=f"{closed}/v1",
        model="m",
        api_key="k",
        tier="local",
        hardware="",
        server_version="",
        purpose_model=[],
        database_url=pg_url,
        turns=2,
        output=tmp_path / "model-matrix.md",
        raw=tmp_path / "model-matrix.json",
    )
    async with restored_model_config(engine):
        assert await mm._main(args) == mm.EX_UNAVAILABLE
    assert not args.output.exists()
    assert not args.raw.exists()
