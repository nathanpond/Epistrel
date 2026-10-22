"""The model matrix tool end to end against a local OpenAI-compatible server (#54): protocol,
capability per purpose, E-06, and timed Lite turns for both rows, then the files it writes.
On the stub adapter, scripted deadline faults show up in the call breakdown (#303), and P-08's
calls assertion holds on them (#308).

The real measurement runs against LM Studio on the owner's machine (``test_p_08``, marked
``live_model``); this run proves the tool's plumbing in CI with a 3-turn row."""

from __future__ import annotations

import argparse
import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.context import RequestContext
from epistrel.core.errors import RevisionConflict
from epistrel.core.profiles.story import ProfileOverridesPatch
from epistrel.core.seeds.profiles import STRUCTURED_M4_PROFILE_ID
from epistrel.engine import Engine
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.models.stub import FaultKind, StubAdapter
from epistrel.tools import model_matrix as mm
from tests.auth_support import auth_settings
from tests.model_support import config_key, mock_openai, restored_model_config
from tests.perf.test_p_08 import check_rows

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


async def test_p_08_holds_on_scripted_flagged_and_deadline_turns(
    pg_url: str, engine: AsyncEngine
) -> None:
    """#308: P-08's turns as P-08 measures them (``mm.measure_turns``), on the stub with the
    deadline run's faults: in the minors row the output check flags the draft in measured
    turns 2 and 4 and the deadline cuts the regeneration or its re-check. The folded mean is
    ``guardrail_check 2.2``, so P-08's old assertion fails, while ``check_rows`` passes:
    every turn kept its row's design and recorded its call cap, and stayed within it.

    Complement asserted: the policies-off row of the same run has no extra call."""
    settings = auth_settings(pg_url, model_adapter="stub", config_key=config_key())
    async with restored_model_config(engine):
        epistrel = Engine(settings, db=engine)
        try:
            await mm.configure(epistrel, STUB_TARGET)
            rows: dict[str, list[mm.TurnCalls]] = {}
            for policies in mm.POLICY_SETS:
                driver = DeadlineDriver(epistrel.models.stub)
                _, rows[policies] = await mm.measure_turns(epistrel, driver, policies, MEASURED)
        finally:
            await epistrel.models.aclose()
    assert mm.calls_per_turn(rows["minors"])["guardrail_check"] == 2.2
    assert all(turn.max_calls for turn in rows["minors"])
    breakdowns = check_rows(rows)
    assert breakdowns["minors"].faulted_turns == [2, 4]
    assert breakdowns["minors"].extra_calls == {"generation": 2, "guardrail_check": 1}
    assert breakdowns["off"].extra_calls == {}


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


async def test_structured_rows_run_under_a_recorded_deadline_override(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#454 (the owner's option (a)): a structured row's story overrides ``budgets.deadline_ms``
    with the matrix's measurement deadline (``TURN_DEADLINE_S``), so a slow tier's structured
    turn is measured rather than cut by the ``Structured (M4)`` profile's 45 s, and the row
    records the override.

    Complement asserted: the Lite rows run under no override and record none, and the
    structured story's profile is still ``Structured (M4)`` (only the deadline is overridden)."""
    settings = auth_settings(pg_url, model_adapter="stub", config_key=config_key())
    async with restored_model_config(engine):
        epistrel = Engine(settings, db=engine)
        try:
            rows = await mm.run_matrix(
                epistrel, STUB_TARGET, turns=1, turn_kinds=("lite", "structured")
            )
            driver = mm.StructuredDriver()
            await mm.measure_turns(epistrel, driver, "off", 1)
            config = await epistrel.get_effective_config(
                RequestContext(principal=mm.PRINCIPAL), driver.story_id
            )
        finally:
            await epistrel.models.aclose()
    assert rows is not None
    overrides = {(r.turn_kind, r.policies): r.deadline_override_ms for r in rows}
    measured = mm.MEASUREMENT_DEADLINE_MS
    assert measured == int(mm.TURN_DEADLINE_S * 1000) == 300_000
    assert overrides == {
        ("lite", "off"): None,
        ("lite", "minors"): None,
        ("structured", "off"): measured,
        ("structured", "minors"): measured,
    }
    assert config.profile.id == STRUCTURED_M4_PROFILE_ID
    assert config.overrides.settings == {"budgets": {"deadline_ms": measured}}
    assert config.effective["budgets"]["deadline_ms"] == measured


@dataclass
class FailingDriver:
    """``RespondDriver`` whose ``fail_at`` turn (0 = the warm-up) raises the deadline's
    ``model_unavailable`` in the minors row, as #454's live structured turn did."""

    fail_at: int
    inner: mm.RespondDriver = field(default_factory=mm.RespondDriver)
    name: str = mm.TURN_PATH
    played: int = -1
    faulty: bool = False
    conflict: bool = False

    async def prepare(self, engine: Engine, policies: mm.PolicySet) -> None:
        await self.inner.prepare(engine, policies)
        self.faulty = policies == "minors"

    async def turn(self, said: str) -> mm.TurnCalls:
        self.played += 1
        if self.faulty and self.played == self.fail_at and self.conflict:
            raise RevisionConflict("changed", {"reason": "budget"})
        if self.faulty and self.played == self.fail_at:
            error = ModelUnavailable("extraction_backstop", "deadline")
            error.details["structural_errors"] = ["budget_exhausted"]
            raise error
        return await self.inner.turn(said)


async def test_a_row_failing_partway_keeps_the_finished_rows(
    pg_url: str, engine: AsyncEngine
) -> None:
    """#454: the minors row's second measured turn fails on the deadline. ``run_matrix``
    raises ``MatrixInterrupted`` carrying the policies-off row, which finished, and the
    minors row's failure: the turn reached, the error code, purpose, reason and structural
    errors.

    Complement asserted: no row for the failed minors row is in ``rows`` (no partial timings)."""
    settings = auth_settings(pg_url, model_adapter="stub", config_key=config_key())
    async with restored_model_config(engine):
        epistrel = Engine(settings, db=engine)
        try:
            with pytest.raises(mm.MatrixInterrupted) as stopped:
                await mm.run_matrix(
                    epistrel, STUB_TARGET, turns=3, driver_factory=lambda: FailingDriver(2)
                )
        finally:
            await epistrel.models.aclose()
    assert [(r.policies, r.turns) for r in stopped.value.rows] == [("off", 3)]
    failure = stopped.value.failure
    assert (failure.policies, failure.turn_kind, failure.turn) == ("minors", "lite", 2)
    assert (failure.code, failure.purpose, failure.reason) == (
        "model_unavailable",
        "extraction_backstop",
        "deadline",
    )
    assert failure.structural_errors == ["budget_exhausted"]
    assert failure.deadline_override_ms is None


async def test_a_turn_conflict_stops_the_row_like_a_model_failure(
    pg_url: str, engine: AsyncEngine
) -> None:
    """P-08's structured row: a turn whose read set changed and whose budget cannot pay to
    generate again (409 ``revision_conflict``) stops its row as a model failure does (#454),
    rather than ending the run with nothing written."""
    settings = auth_settings(pg_url, model_adapter="stub", config_key=config_key())
    async with restored_model_config(engine):
        epistrel = Engine(settings, db=engine)
        try:
            with pytest.raises(mm.MatrixInterrupted) as stopped:
                await mm.run_matrix(
                    epistrel,
                    STUB_TARGET,
                    turns=3,
                    driver_factory=lambda: FailingDriver(1, conflict=True),
                )
        finally:
            await epistrel.models.aclose()
    assert [(r.policies, r.turns) for r in stopped.value.rows] == [("off", 3)]
    failure = stopped.value.failure
    assert (failure.policies, failure.turn) == ("minors", 1)
    assert (failure.code, failure.purpose, failure.reason) == ("revision_conflict", None, "budget")


def row_of(kind: mm.TurnKind, policies: mm.PolicySet, **changes: object) -> mm.Row:
    values: dict[str, object] = {
        "tier": "local",
        "model": "m",
        "policies": policies,
        "measured_at": "2026-10-20",
        "sha": "abc1234",
        "hardware": "",
        "server_version": "",
        "turn_path": mm.TURN_PATH if kind == "lite" else mm.STRUCTURED_PATH,
        "protocol": True,
        "protocol_reason": None,
        "capability": {},
        "e06": {},
        "turns": 1,
        "p50_ms": 1000,
        "p95_ms": 1000,
        "calls_per_turn": {"generation": 1.0},
        "latencies_ms": [1000],
        "turn_kind": kind,
    }
    values.update(changes)
    return mm.Row(**values)  # type: ignore[arg-type]


async def test_main_writes_the_finished_rows_and_marks_the_failure(
    pg_url: str, engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#454: when a row fails partway, the tool writes the rows that finished, lists the
    failed row under **Failed runs** (``failed_rows`` in the JSON, with the error's structural
    errors), and exits 1. The tier's
    earlier row of the failed key keeps its timings.

    Complement asserted: a later run that measures the failed row clears its failure mark."""
    finished = row_of("lite", "off")
    earlier = row_of("structured", "minors", latencies_ms=[9000])
    failure = mm.RowFailure(
        tier="local",
        model="m",
        policies="minors",
        turn_kind="structured",
        measured_at="2026-10-20",
        sha="abc1234",
        turn_path=mm.STRUCTURED_PATH,
        turn=3,
        code="model_unavailable",
        purpose="generation",
        reason="deadline",
        deadline_override_ms=mm.MEASUREMENT_DEADLINE_MS,
        structural_errors=["budget_exhausted"],
    )
    raw, markdown = tmp_path / "model-matrix.json", tmp_path / "model-matrix.md"
    mm.write_matrix(mm.merge([], [earlier]), markdown, raw)

    async def interrupted(*_: object, **__: object) -> list[mm.Row]:
        raise mm.MatrixInterrupted([finished], failure)

    monkeypatch.setattr(mm, "run_matrix", interrupted)
    args = argparse.Namespace(
        base_url="http://stub.invalid/v1",
        model="m",
        api_key="k",
        tier="local",
        hardware="",
        server_version="",
        purpose_model=[],
        database_url=pg_url,
        turns=1,
        output=markdown,
        raw=raw,
    )
    async with restored_model_config(engine):
        assert await mm._main(args) == mm.EX_UNAVAILABLE
    data = json.loads(raw.read_text("utf-8"))
    # Sorted by key: the structured minors row (its earlier timings), then the Lite off row.
    assert [r["latencies_ms"] for r in data["rows"]] == [[9000], [1000]]
    assert data["failed_rows"] == [dataclasses.asdict(failure)]
    text = markdown.read_text("utf-8")
    assert "## Failed runs" in text
    assert (
        "| local: m (structured turns) | minors policy on | 2026-10-20 | `abc1234` | 3 | "
        "model_unavailable, purpose `generation`, reason `deadline`; `budget_exhausted` | "
        f"{mm.STRUCTURED_PATH}; turn deadline 300 s (override) |"
    ) in text
    assert "**Turn deadline override (#454).**" in text

    async def remeasured(*_: object, **__: object) -> list[mm.Row]:
        return [row_of("structured", "minors")]

    monkeypatch.setattr(mm, "run_matrix", remeasured)
    async with restored_model_config(engine):
        assert await mm._main(args) == 0
    assert "failed_rows" not in json.loads(raw.read_text("utf-8"))
    assert "## Failed runs" not in markdown.read_text("utf-8")
