"""#913: a run's own settings patch (``python -m epistrel.evals run --dial PATH=VALUE``).

The dials form one merge patch every structured scratch story takes over the run's preset;
the run's fingerprint and report name it, a run without dials is unchanged, and a dialled run
never certifies a preset or merges into another run."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.evals import AdapterConfig, CaseModel, Outcome, Suite, cli, run
from epistrel.evals import report as reports
from epistrel.evals.gate import Metric, Rule
from epistrel.evals.presets import PRESETS
from epistrel.evals.runner import CaseContext, default_run_id, run_fingerprint, run_suites
from epistrel.evals.scenes import structured
from tests.evals.support import stub_engine

FULL = {"memory": {"episodic": {"profile_scope": "full"}}}
SEEN = "dials.profile_scope_seen"


def test_a_dial_is_a_dotted_path_and_a_json_value() -> None:
    """``PATH=VALUE`` flags make one nested merge patch: a JSON value as JSON, anything else as
    the string written, a later flag for the same path winning. A flag without ``=`` or with an
    empty path segment is refused, and so is a patch the preset's settings reject (an unknown
    setting, a value out of its range): exit 2 before any run.

    Complement asserted: the accepted patch validates over the preset (the refusal is the
    settings', not the parser's)."""
    assert cli.dial_patch(
        [
            "memory.episodic.profile_scope=full",
            "memory.episodic.retrieval_token_budget=900",
            "memory.episodic.retrieval_token_budget=1200",
        ]
    ) == {"memory": {"episodic": {"profile_scope": "full", "retrieval_token_budget": 1200}}}
    for bad in ("memory.episodic.profile_scope", "memory..profile_scope=full", "=full"):
        with pytest.raises(cli.DialInvalid):
            cli.dial_patch([bad])
    with pytest.raises(cli.DialInvalid):
        cli.checked_dials(["memory.episodic.profile_scope=everything"], "full")
    with pytest.raises(cli.DialInvalid):
        cli.checked_dials(["memory.episodic.no_such_setting=1"], "full")
    assert cli.checked_dials([], "full") is None
    patch = cli.checked_dials(["memory.episodic.profile_scope=full"], "full")
    assert patch == FULL
    assert PRESETS["full"].effective(patch).memory.episodic.profile_scope == "full"
    argv = ["run", "--adapter", "stub", "--suite", "smoke", "--dial", "nonsense"]
    assert cli.main(argv) == cli.EX_USAGE


def test_a_dialled_run_has_its_own_fingerprint() -> None:
    """The run's fingerprint without dials is what it was (``run_fingerprint(model)``), and a
    patch that changes nothing (the default's own value) leaves it too; a patch that changes a
    setting gives another fingerprint and so another run id.

    Complement asserted: the dialled fingerprint is the patch's alone (the same patch, the
    same fingerprint)."""
    plain = run_fingerprint("stub")
    assert run_fingerprint("stub", None) == plain
    assert run_fingerprint("stub", {"memory": {"episodic": {"profile_scope": "message"}}}) == plain
    dialled = run_fingerprint("stub", FULL)
    assert dialled != plain
    when = datetime(2026, 11, 14, tzinfo=UTC)
    assert default_run_id(dialled, when, "full") != default_run_id(plain, when, "full")
    assert run_fingerprint("stub", json.loads(json.dumps(FULL))) == dialled
    assert reports.dial_leaves(FULL) == [("memory.episodic.profile_scope", "full")]


async def test_a_certification_run_takes_no_dials() -> None:
    """``run_suites`` with a certification profile refuses dials before any case: a dialled
    run never certifies a preset."""
    with pytest.raises(ValueError, match="takes no --dial"):
        await run_suites([], AdapterConfig(), [1], profile=PRESETS["full"], dials=FULL)


def test_a_dialled_run_never_merges_into_another(tmp_path: Path) -> None:
    """A ``--suite`` rerun into a run (``--run-id``) whose dials differ exits 2 before any
    case: one run never mixes two configurations.

    Complement asserted: the run's id and JSON are found (the refusal is the dials', not a
    missing run)."""
    prior = {
        "run_id": "full-abc-2026-11-14",
        "started_at": "2026-11-14T00:00:00+00:00",
        "model": "stub",
        "fingerprint": "sha256:" + "0" * 64,
        "fingerprint_profile": "Full v1",
        "adapter": "stub",
        "engine_sha": "0" * 7,
        "host": {"kind": "ci"},
        "seed_base": 1,
        "seeds": [1],
    }
    (tmp_path / "full-abc-2026-11-14.json").write_text(json.dumps(prior), encoding="utf-8")
    assert reports.Report.model_validate(prior).dials is None
    argv = [
        "run",
        "--adapter",
        "stub",
        "--suite",
        "smoke",
        "--run-id",
        "full-abc-2026-11-14",
        "--json-dir",
        str(tmp_path),
        "--dial",
        "memory.episodic.profile_scope=full",
    ]
    assert cli.main(argv) == cli.EX_USAGE


class _SeesTheScope(Suite):
    """One case: a scratch story on the run's preset (``scenes.structured``), its effective
    ``profile_scope`` recorded."""

    name = "dials_probe"
    title = "the run's dials reach a scratch story"
    metrics = (Metric(SEEN, Rule.informational),)
    seen: ClassVar[list[str]] = []

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        cast = [
            CharacterSpec(name="Wren", definition="A traveller.", true_age=29, is_player=True),
            CharacterSpec(name="Maren", definition="The innkeeper.", true_age=46),
        ]
        created = await ctx.story(StorySpec(characters=cast))
        await structured(ctx.engine, ctx.principal, created.story_id)
        async with ctx.engine.db.connect() as conn:
            found = await effective_configuration(conn, created.story_id)
        self.seen.append(found.settings.memory.episodic.profile_scope)
        return Outcome({SEEN: True})


@pytest.mark.integration
async def test_the_dials_reach_every_scratch_story(pg_url: str, engine: AsyncEngine) -> None:
    """Run with ``dials``, a suite's scratch story on the run's preset has the dialled setting,
    the report names the dials, and its fingerprint is the dialled configuration's.

    Complement asserted: the same run without dials has the preset's own value (``message``),
    no dials in its report and the preset's fingerprint."""
    suite = _SeesTheScope()
    cases: Mapping[str, Any] = {suite.name: [CaseModel(id="dials-0001")]}
    _SeesTheScope.seen.clear()
    async with stub_engine(pg_url, engine) as lib:
        dialled = await run(suite, AdapterConfig(), [1], engine=lib, cases=cases, dials=FULL)
        plain = await run(suite, AdapterConfig(), [1], engine=lib, cases=cases)
    assert dialled.harness_errors == plain.harness_errors == []
    assert _SeesTheScope.seen == ["full", "message"]
    assert dialled.dials == FULL
    assert plain.dials is None
    assert dialled.fingerprint == run_fingerprint("stub", FULL)
    assert plain.fingerprint == run_fingerprint("stub")
    assert '**Dials:** `memory.episodic.profile_scope = "full"`' in reports.render_markdown(dialled)
    assert "**Dials:**" not in reports.render_markdown(plain)
