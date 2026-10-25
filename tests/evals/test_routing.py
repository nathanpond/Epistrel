"""The eval runner's per-purpose routing and scripted blocks (#179): a real-model run that puts
only ``generation`` on the endpoint (``--real-purpose generation``), every other purpose and
every scripted prelude on the run's own stub.

The "endpoint" here is a stub adapter standing in for LM Studio (the engine's own, which a
real-model run wraps): the harness path a local run takes, offline."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, cli, runner
from epistrel.evals.runner import RoutedAdapter, routed_purposes, run_fingerprint
from epistrel.evals.suites.priv_think_leak import ThinkLeakCase
from epistrel.orchestrator.models.stub import StubAdapter
from tests.eval.suite_support import SETS
from tests.evals.support import stub_engine

pytestmark = pytest.mark.integration

LOCAL = AdapterConfig(
    adapter="openai",
    base_url="http://endpoint.invalid/v1",
    model="local-model",
    real_purposes=("generation",),
)


def test_the_cli_takes_real_purposes_and_skips() -> None:
    """``--real-purpose`` (repeatable) and ``--skip``; without ``--real-purpose`` every purpose
    is the model's, and a stub run routes nothing."""
    args = cli.parser().parse_args(
        ["run", "--all", "--skip", "q04", "--real-purpose", "generation", "--model", "m"]
    )
    assert (args.skip, args.real_purposes) == (["q04"], ["generation"])
    assert routed_purposes(cli._config(args, "openai")) == ["generation"]
    plain = cli.parser().parse_args(["run", "--suite", "q04", "--model", "m"])
    assert routed_purposes(cli._config(plain, "openai")) is None
    assert routed_purposes(AdapterConfig(real_purposes=("generation",))) is None
    assert run_fingerprint("m", real_purposes=["generation"]) != run_fingerprint("m")


async def test_a_routed_run_sends_only_generation_and_never_the_prelude(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The private-thought suite on a routed "real-model" run: the endpoint gets the measured
    turns' ``generation`` calls only, never a scripted prelude (its render and the rest of that
    turn reach the run's stub, which answers the prelude it was scripted with); every other
    purpose goes to the stub too; the run's fingerprint names the stub for the rest; the
    engine's adapter is restored afterwards.

    Complement asserted: without a scripted block the same render would have reached the
    endpoint (``routes_real``)."""
    made: list[StubAdapter] = []

    def stub_factory(*args: Any, **kwargs: Any) -> StubAdapter:
        found = StubAdapter(*args, **kwargs)
        made.append(found)
        return found

    monkeypatch.setattr(runner, "StubAdapter", stub_factory)
    suite = SUITES["priv_think_leak"]
    cases = [c for c in suite.load(SETS) if isinstance(c, ThinkLeakCase) and not c.control][:3]
    async with stub_engine(pg_url, engine) as lib:
        endpoint = lib.models.stub
        # ``run_suite`` under ``routed``, as ``run_suites`` plays it after the preflight (which
        # would write the endpoint into the shared test database's model configuration).
        with runner.routed(lib, LOCAL):
            job = runner.SuiteRun(
                engine=lib,
                config=LOCAL,
                seeds=[20261019],
                run_id="routed-test",
                cases={suite.name: cases},
            )
            result = await runner.run_suite(suite, job)
        assert lib.models.adapter is endpoint
        # (``stub_engine`` forgets the endpoint's calls on leaving.)
        turns = [c for c in endpoint.calls if len(c.messages) > 1]
    assert result.errors == []
    # #181: the suite runs the seeded profile itself (no dials), so it reports the run's own
    # fingerprint, which names the stub for every routed-away purpose.
    assert (suite.dials, result.fingerprint) == (None, None)
    assert run_fingerprint("local-model", real_purposes=["generation"]) != run_fingerprint(
        "local-model"
    )
    (routed_stub,) = made
    assert turns
    assert {c.purpose for c in turns} == {"generation"}
    sent = ["\n".join(m.content for m in c.messages) for c in turns]
    for case in cases:
        assert any(case.inputs.player_input in text for text in sent)
        # The prelude's own render never reached the endpoint (the measured turn's prompt holds
        # the prelude's line in its working memory, never its render request).
        assert not any(
            case.inputs.prelude in text and case.inputs.player_input not in text for text in sent
        )
    stubbed = ["\n".join(m.content for m in c.messages) for c in routed_stub.calls]
    assert all(any(c.inputs.prelude in text for text in stubbed) for c in cases)
    assert {c.purpose for c in routed_stub.calls} >= {"generation"}
    adapter = RoutedAdapter(endpoint, routed_stub, ["generation"])
    assert adapter.routes_real("generation")
    assert not adapter.routes_real("extraction_backstop")
    with runner.CaseContext(lib, runner.Scratch(lib, "x"), 1, "x").scripted():
        assert not adapter.routes_real("generation")
