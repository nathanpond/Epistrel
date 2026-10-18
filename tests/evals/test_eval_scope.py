"""The Model Access Layer's eval scope, the stub's per-case answers, the ``seed`` parameter and
the ``quality_judge`` purpose (#134), without a database."""

from __future__ import annotations

from pathlib import Path

import pytest

from epistrel.orchestrator.models import registry
from epistrel.orchestrator.models.access import ModelAccessLayer, eval_scope
from epistrel.orchestrator.models.adapter import Message, ModelParams, eval_case_of
from epistrel.orchestrator.models.openai_compatible import request_body
from epistrel.orchestrator.models.registry import ContentCategory as C
from tests.auth_support import auth_settings

USER = [Message(role="user", content="Hello.")]
WITH_SYSTEM = [Message(role="system", content="Be brief."), *USER]


def _layer() -> ModelAccessLayer:
    return ModelAccessLayer(
        auth_settings("postgresql+psycopg://u:p@localhost:5432/x", model_adapter="stub")
    )


async def test_a_call_in_an_eval_scope_names_its_case_and_seed() -> None:
    """Inside ``eval_scope`` the system message ends with the case's ``X-Eval-Case`` line (a
    system message of its own when the prompt has none) and the call carries the run's seed;
    a seed the call sets itself wins. Complement: outside the scope nothing is added."""
    layer = _layer()
    stub = layer.stub
    await layer.complete("generation", WITH_SYSTEM)
    with eval_scope("case-7", 20261008):
        await layer.complete("generation", WITH_SYSTEM)
        await layer.complete("generation", USER, ModelParams(seed=5))
    outside, marked, own_seed = stub.calls
    assert outside.messages == tuple(WITH_SYSTEM)
    assert outside.params.seed is None
    assert marked.messages[0].content == "Be brief.\n\nX-Eval-Case: case-7"
    assert marked.messages[1:] == tuple(USER)
    assert marked.params.seed == 20261008
    assert own_seed.messages[0] == Message(role="system", content="X-Eval-Case: case-7")
    assert own_seed.params.seed == 5
    assert eval_case_of(marked.messages) == "case-7"
    assert eval_case_of(outside.messages) is None
    with pytest.raises(ValueError, match="one non-empty token"), eval_scope("two words"):
        pass


async def test_the_stub_answers_per_case_and_seed() -> None:
    """``stub.case`` answers the calls of one case (one purpose, one seed, when given); the
    most specific answer wins. Complement: another case, or no scope, gets the default."""
    layer = _layer()
    stub = layer.stub
    stub.case("c1", "any seed")
    stub.case("c1", "seed two", seed=2)
    stub.case("c1", "summary only", purpose="lite_summary")
    with eval_scope("c1", 1):
        assert (await layer.complete("generation", USER)).text == "any seed"
        assert (await layer.complete("lite_summary", USER)).text == "summary only"
    with eval_scope("c1", 2):
        assert (await layer.complete("generation", USER)).text == "seed two"
    with eval_scope("c10", 2):
        assert (await layer.complete("generation", USER)).text.startswith("[stub generation ")
    assert (await layer.complete("generation", USER)).text.startswith("[stub generation ")
    stub.reset()
    with eval_scope("c1", 1):
        assert (await layer.complete("generation", USER)).text.startswith("[stub generation ")


def test_seed_is_sent_as_the_openai_seed() -> None:
    body = request_body("m", USER, ModelParams(seed=42), None)
    assert body["seed"] == 42
    assert "seed" not in request_body("m", USER, ModelParams(), None)


def test_quality_judge_is_delivered_outside_stories() -> None:
    """``quality_judge`` is a delivered purpose for the eval harness only: off the hot path,
    not story-scoped (no profile routes it, no fingerprint names it), defaulting to the
    ``default`` row, and refusing every story-content category."""
    judge = registry.get_purpose("quality_judge")
    assert "quality_judge" not in registry.RESERVED
    assert (judge.hot_path, judge.story_scoped) == (False, False)
    assert judge.default_model_key == registry.DEFAULT_MODEL_KEY
    assert "quality_judge" not in registry.STORY_PURPOSES
    story_content = {
        C.RECENT_MESSAGE,
        C.PLAYER_INPUT,
        C.PINNED_MEMORY,
        C.ROLLING_SUMMARY,
        C.CHARACTER_DEFINITION,
        C.CUSTOM_INSTRUCTIONS,
        C.SCENARIO_TEXT,
        C.DIRECTOR_NOTES,
        C.CHECKED_TEXT,
        C.SUBJECT_TRUE_AGE,
    }
    assert story_content <= judge.never_receives
    assert registry.schema_categories(judge.input_schema) == {C.EVAL_TEXT}
    assert all(
        C.EVAL_TEXT in p.never_receives
        for n, p in registry.DELIVERED.items()
        if n != "quality_judge"
    )


def test_nothing_in_the_engine_imports_the_harness() -> None:
    """The harness never runs in a turn: the import-linter contract forbids Core, the
    Orchestrator, the API, the Engine and observability from importing ``epistrel.evals``, and
    the real tree has no such import. Complement: the harness itself imports the Engine."""
    import ast
    import tomllib

    from tests.conftest import REPO_ROOT

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    contracts = {c["name"]: c for c in config["tool"]["importlinter"]["contracts"]}
    contract = contracts["nothing-in-the-engine-imports-the-eval-harness"]
    assert contract["type"] == "forbidden"
    assert contract["forbidden_modules"] == ["epistrel.evals"]
    assert set(contract["source_modules"]) == {
        "epistrel.core",
        "epistrel.orchestrator",
        "epistrel.api",
        "epistrel.engine",
        "epistrel.observability",
    }
    src = REPO_ROOT / "src" / "epistrel"

    def imported(path: Path) -> set[str]:
        tree = ast.parse(path.read_text())
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found |= {alias.name for alias in node.names}
        return found

    engine_files = [src / "engine.py"]
    for package in ("core", "orchestrator", "api", "observability"):
        engine_files += sorted((src / package).rglob("*.py"))
    offenders = [
        str(p) for p in engine_files if any(m.startswith("epistrel.evals") for m in imported(p))
    ]
    assert offenders == []
    assert "epistrel.engine" in imported(src / "evals" / "runner.py")
