"""E-06, M11's part (#939, NFR-QUAL-7): repeatability counts only on a deterministic host
(ADR-202, the owner's option (b)), the structure-miss code, and the tally tool."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from tools.e06_tally import code_of, refusals, render, shape, tally

from epistrel.evals.report import Host
from epistrel.evals.runner import AdapterConfig, deterministic_of
from epistrel.evals.suites import Outcome
from epistrel.evals.suites import e06_conformance as e06
from epistrel.evals.suites.e06_conformance import (
    CONFORMANCE,
    NOT_DETERMINISTIC_NOTE,
    REPEATABLE,
    ConformanceCase,
    E06Suite,
    conforms,
    structure_code,
)

pytestmark = pytest.mark.part("M11", "NFR-QUAL-7")


def test_e_06_m11__host_flag() -> None:
    """A hosted endpoint is not deterministic by default (api.anthropic.com), LM Studio and the
    stub are, and the flag overrides either way.

    Complement asserted: an explicit ``True`` on the hosted endpoint makes it deterministic."""
    hosted = AdapterConfig(adapter="openai", base_url="https://api.anthropic.com/v1", model="m")
    local = AdapterConfig(adapter="openai", base_url="http://localhost:1235/v1", model="m")
    assert deterministic_of(hosted) is False
    assert deterministic_of(local) is True
    assert deterministic_of(AdapterConfig()) is True
    assert (
        deterministic_of(
            AdapterConfig(adapter="openai", base_url=hosted.base_url, deterministic=True)
        )
        is True
    )
    assert (
        deterministic_of(
            AdapterConfig(adapter="openai", base_url=local.base_url, deterministic=False)
        )
        is False
    )


@pytest.mark.parametrize(
    ("repeated", "deterministic", "expected"),
    [(False, False, True), (False, True, False), (True, True, True), (None, True, True)],
)
def test_e_06_m11__conforms(repeated: bool | None, deterministic: bool, expected: bool) -> None:
    """A differing repeat leaves conformance alone only on a non-deterministic host; on a
    deterministic one it fails the case (the complement)."""
    assert (
        conforms(structure=True, length=True, repeated=repeated, deterministic=deterministic)
        is expected
    )


def test_e_06_m11__conforms_needs_structure_and_length() -> None:
    """Off the repeat rule, a structure or length miss still fails the case on any host."""
    assert conforms(structure=False, length=True, repeated=None, deterministic=False) is False
    assert conforms(structure=True, length=False, repeated=None, deterministic=False) is False


def _case(*, repeat: bool) -> ConformanceCase:
    return ConformanceCase.model_validate(
        {
            "id": "generation_conformance-repeat-9999",
            "family": "repeat",
            "origin": "authored",
            "inputs": {
                "say": "Hello.",
                "prompt_params": {"length": "short", "temperature": 0},
                "repeat": repeat,
            },
            "expected": "valid",
            "stub_answer": "{}",
        }
    )


async def _evaluate(monkeypatch: pytest.MonkeyPatch, *, deterministic: bool) -> Outcome:
    """E-06's ``evaluate`` on a repeat case whose two renders differ (``play`` and
    ``render_output`` faked: a valid, short turn)."""
    beat = SimpleNamespace(
        kind="beat_say", channel="say", text="Welcome.", beat=0, operation_id=None
    )
    turn = SimpleNamespace(
        beats=[beat],
        message=SimpleNamespace(text="Welcome."),
        degradation=[],
        degradation_details=[],
    )
    keys = iter([((0, "say", "Welcome."),), ((0, "say", "Hello there."),)])

    async def play(*_: Any, **__: Any) -> Any:
        return turn

    async def render_output(*_: Any) -> Any:
        return next(keys)

    monkeypatch.setattr(e06, "play", play)
    monkeypatch.setattr(e06, "render_output", render_output)
    ctx: Any = SimpleNamespace(deterministic=deterministic)
    return await E06Suite().evaluate(ctx, _case(repeat=True))


async def test_e_06_m11__non_deterministic_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a host flagged non-deterministic a differing repeat render is counted in
    ``e06.repeatable`` but leaves ``nfr_qual_7.conformance`` passing, and the report gets the
    note.

    Complement asserted: on a deterministic host the same difference fails conformance, and no
    note is added."""
    loose = await _evaluate(monkeypatch, deterministic=False)
    assert loose.observations[REPEATABLE] is False
    assert loose.observations[CONFORMANCE] is True
    assert loose.detail["deterministic"] is False
    assert E06Suite().summarize([loose], "m").notes == (NOT_DETERMINISTIC_NOTE,)
    strict = await _evaluate(monkeypatch, deterministic=True)
    assert strict.observations[REPEATABLE] is False
    assert strict.observations[CONFORMANCE] is False
    assert E06Suite().summarize([strict], "m").notes == ()


def test_e_06_m11__report_says_so() -> None:
    """The reading notes and the host model carry the rule; a host defaults to deterministic
    (an older record reads back unchanged)."""
    assert "ADR-202" in " ".join(E06Suite.reading_notes)
    assert Host(kind="hosted").deterministic is True
    assert Host(kind="hosted", deterministic=False).deterministic is False


def test_e_06_m11__structure_code() -> None:
    """A fallback names its cause and the rejected constraint kinds; otherwise the first
    problem; no code for a valid structure."""
    fallback = "the safe fallback replaced the render"
    assert structure_code([], [], []) is None
    assert structure_code([fallback], ["repair"], ["state_constraint", "uncertain"]) == (
        "fallback:repair:state_constraint,uncertain"
    )
    assert structure_code([fallback], ["repair"], []) == "fallback:repair"
    assert structure_code(["no say or do beat"], [], []) == "no say or do beat"


def _line(observations: Mapping[str, bool | None], **detail: Any) -> dict[str, Any]:
    return {"suite": "e06", "observations": observations, "detail": detail}


def test_e_06_m11__tally() -> None:
    """The tally counts misses by part and structure miss by code, reading an older line's
    fallback causes as its code, and a model error as ``model_error:<code>:<reason>``."""
    miss = {CONFORMANCE: False, "e06.structure_valid": False, "e06.length_honoured": True}
    lines = [
        _line({CONFORMANCE: True, "e06.structure_valid": True, "e06.length_honoured": True}),
        _line(miss, structure_code="fallback:repair:uncertain"),
        _line(miss, fallback_causes=["repair"], problems=["the safe fallback replaced the render"]),
        _line(
            {CONFORMANCE: False, "e06.structure_valid": False, "e06.length_honoured": False},
            model_error="model_unavailable",
            reason="invalid_structure",
        ),
        _line(
            {
                CONFORMANCE: True,
                "e06.structure_valid": True,
                "e06.length_honoured": True,
                REPEATABLE: False,
            }
        ),
        {"suite": "e12", "observations": {}, "detail": {}},
    ]
    result = tally(lines)
    assert result["outcomes"] == 5
    assert result["parts"]["structure"] == (3, 5)
    assert result["parts"]["length"] == (1, 5)
    assert result["parts"]["repeat"] == (1, 1)
    assert result["codes"] == {
        "fallback:repair:uncertain": 1,
        "fallback:repair": 1,
        "model_error:model_unavailable:invalid_structure": 1,
    }
    assert code_of(lines[0]) is None
    assert "`fallback:repair` | 1 | 20.00% | yes" in render(result, None)


def test_e_06_m11__refusal_shapes() -> None:
    """A retried render call's correction folds to a stable shape (numbers and parenthesised
    details folded); first attempts and other purposes are not counted."""
    correction = (
        "Your previous answer could not be used: the object: Value error, beats.0.effects.1."
        "state: the world clock moves only by a time advance. Reply again with only the JSON "
        "object, and nothing else."
    )
    assert (
        shape(correction) == "beats.N.effects.N.state: the world clock moves only by a time advance"
    )
    assert (
        shape(
            "Your previous answer could not be used: it is not valid JSON (Expecting value). Reply "
            "again with only the JSON object, and nothing else."
        )
        == "it is not valid JSON (…)"
    )
    calls = [
        {
            "suite": "e06",
            "purpose": "generation",
            "attempt": 2,
            "schema": "CharacterRender",
            "messages": [{"role": "user", "content": correction}],
        },
        {
            "suite": "e06",
            "purpose": "generation",
            "attempt": 1,
            "schema": "CharacterRender",
            "messages": [{"role": "user", "content": correction}],
        },
        {
            "suite": "e06",
            "purpose": "extraction_backstop",
            "attempt": 2,
            "schema": "X",
            "messages": [{"role": "user", "content": correction}],
        },
    ]
    assert refusals(calls) == {
        (
            "CharacterRender",
            "beats.N.effects.N.state: the world clock moves only by a time advance",
        ): 1
    }
