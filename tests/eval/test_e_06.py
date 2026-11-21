"""E-06: generation conformance (docs/04).

The M2 part (``test_e_06``): Lite output honours the prompt parameters on a real model. Marked
``live_model``: CI never runs it; the model matrix tool runs the same check
(``capability.check_prompt_params``) and records it per tier, reported, not gated (#54).

The M4 part (``test_e_06__structured``, #157): the ``e06`` suite plays structured turns
(``evals/sets/generation_conformance``) on scratch stories against the stub, scripted per case:
valid say/think/do/latent structure, the requested length and temperature-0 repeatability."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tools.e06_tally import code_of, refusals, render, shape, tally

from epistrel.evals import SUITES, Verdict
from epistrel.evals.report import Host
from epistrel.evals.runner import AdapterConfig, deterministic_of
from epistrel.evals.suites import Outcome
from epistrel.evals.suites import e06_conformance as e06
from epistrel.evals.suites.e06_conformance import (
    CONFORMANCE,
    NOT_DETERMINISTIC_NOTE,
    RELEASED_FALLBACK,
    REPEATABLE,
    ConformanceCase,
    E06Suite,
    conforms,
    fallback_code,
    structure_code,
    within_length,
)
from epistrel.orchestrator.models.capability import E06_THRESHOLD, check_prompt_params
from tests.eval.suite_support import SETS, assert_floors_pass, by_id, strict_verdict, stub_run
from tests.live_support import live_layer

STRUCTURED_SAMPLE = 30


@pytest.mark.live_model
@pytest.mark.part("M2", "FR-RESP-3")
async def test_e_06() -> None:
    """For each ``max_tokens`` length the reply's ``tokens_out`` stays within it, and at
    temperature 0 at least 80 % of 10 samples are identical.

    Complement asserted: the lengths differ, so the cap is what bounds each reply."""
    layer = live_layer()
    try:
        result = await check_prompt_params(layer)
    finally:
        await layer.aclose()
    assert len(set(result.lengths)) > 1
    for cap, tokens_out in result.lengths.items():
        assert tokens_out is not None, f"no answer at max_tokens={cap}"
        assert tokens_out <= cap
    assert result.identical / result.samples >= E06_THRESHOLD


class ThinkOnly(E06Suite):
    """E-06 whose stub renders only a ``think`` beat: no say or do, not a valid turn."""

    def stub_answers(self, case: object) -> dict[str, str]:
        return {
            "generation": json.dumps(
                {"beats": [{"kind": "think", "text": "Nothing to say."}], "introduces": []}
            )
        }


@pytest.mark.integration
@pytest.mark.part("M4", "FR-TURN-1")
@pytest.mark.part("M11", "NFR-QUAL-7")
async def test_e_06__structured(pg_url: str, engine: AsyncEngine) -> None:
    """200 labeled structured turns over every length and temperature, 40 of them repeated at
    temperature 0: on a seeded sample of the stub run every released turn is a valid
    say/think/do structure within its requested length, and each repeat gives the same message
    (conformance 100 % on both seeds; the floor passes).

    Complements asserted: a message past 125 % of its token budget is out of length, and a
    render of a lone ``think`` beat fails the conformance floor under the strict rule."""
    suite = SUITES["e06"]
    cases = [c for c in suite.load(SETS) if isinstance(c, ConformanceCase)]
    assert len(cases) >= 200
    assert sum(c.inputs.repeat for c in cases) >= 30
    result = await stub_run(pg_url, engine, suite, sample=STRUCTURED_SAMPLE)
    assert_floors_pass(result)
    assert by_id(result)[CONFORMANCE].mean == 1.0
    repeats = [c for c in cases if c.inputs.repeat][:2]
    repeated = await stub_run(pg_url, engine, suite, cases=repeats)
    assert by_id(repeated)["e06.repeatable"].mean == 1.0

    assert within_length("x" * 4 * 150, "short")
    assert not within_length("x" * (4 * 188 + 4), "short")
    assert not within_length("x" * 4 * 126, 100)
    complement = await stub_run(pg_url, engine, ThinkOnly(), cases=cases[:3])
    assert by_id(complement)["e06.structure_valid"].mean == 0.0
    assert strict_verdict(complement, CONFORMANCE) is Verdict.failed


@pytest.mark.part("M11", "NFR-QUAL-7")
def test_e_06__m11_host_flag() -> None:
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


@pytest.mark.part("M11", "NFR-QUAL-7")
@pytest.mark.parametrize(
    ("repeated", "deterministic", "expected"),
    [(False, False, True), (False, True, False), (True, True, True), (None, True, True)],
)
def test_e_06__m11_conforms(repeated: bool | None, deterministic: bool, expected: bool) -> None:
    """A differing repeat leaves conformance alone only on a non-deterministic host; on a
    deterministic one it fails the case (the complement)."""
    assert (
        conforms(structure=True, length=True, repeated=repeated, deterministic=deterministic)
        is expected
    )


@pytest.mark.part("M11", "NFR-QUAL-7")
def test_e_06__m11_conforms_needs_structure_and_length() -> None:
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


@pytest.mark.part("M11", "NFR-QUAL-7")
async def test_e_06__m11_non_deterministic_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.part("M11", "NFR-QUAL-7")
def test_e_06__m11_report_says_so() -> None:
    """The reading notes and the host model carry the rule; a host defaults to deterministic
    (an older record reads back unchanged)."""
    assert "ADR-202" in " ".join(E06Suite.reading_notes)
    assert Host(kind="hosted").deterministic is True
    assert Host(kind="hosted", deterministic=False).deterministic is False


@pytest.mark.part("M11", "NFR-QUAL-7")
def test_e_06__m11_structure_code() -> None:
    """A structure miss is coded by its first render problem, a released fallback by its cause
    and the rejected constraint classes (ADR-203); no code for neither."""
    assert structure_code([]) is None
    assert structure_code(["no say or do beat", "a beat without text"]) == "no say or do beat"
    assert fallback_code([], []) is None
    assert fallback_code(["repair"], ["state_constraint", "uncertain"]) == (
        "fallback:repair:state_constraint,uncertain"
    )
    assert fallback_code(["repair"], []) == "fallback:repair"


def _beat(channel: str, text: str, beat: int) -> SimpleNamespace:
    return SimpleNamespace(
        kind=f"beat_{channel}", channel=channel, text=text, beat=beat, operation_id=None
    )


def _turn(*beats: SimpleNamespace, fallback: bool = False) -> SimpleNamespace:
    cause = [SimpleNamespace(step="safe_fallback", cause="repair")] if fallback else []
    return SimpleNamespace(
        beats=list(beats),
        message=SimpleNamespace(text=" ".join(b.text for b in beats) or "…"),
        degradation=["safe_fallback"] if fallback else [],
        degradation_details=cause,
    )


@pytest.mark.part("M11", "NFR-QUAL-7")
def test_e_06__m11_render_structure() -> None:
    """ADR-203: structure is judged on the model's render. A say beat the safe fallback
    replaced is read by its original text, and a think beat the ladder dropped still counts
    as a think, so the render is valid where the released turn is not.

    Complement asserted: a render whose only say/do was empty, or that rendered only a
    (dropped) think, is still a structure miss."""
    from epistrel.evals.suites.structured_turns import render_problems, structure_problems

    released: Any = _turn(_beat("say", "I can't say more.", 1), fallback=True)
    originals = {1: "The lamp was lit at dusk.", 2: "She hides the key."}
    assert structure_problems(released) == ["the safe fallback replaced the render"]
    assert render_problems(released, originals) == []
    assert render_problems(released, {1: "  "}) == ["a beat without text"]
    only_think: Any = _turn(_beat("think", "", 1))
    only_think.beats = []
    only_think.message.text = "…"
    assert render_problems(only_think, {1: "Hidden."}) == ["no say or do beat"]


@pytest.mark.part("M11", "NFR-QUAL-7")
async def test_e_06__m11_fallback_conforms(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid render the safe fallback replaced conforms; ``e06.released_fallback`` counts it,
    and its line names the fallback (``fallback_code``) and keeps the released turn's problems.

    Complement asserted: the same turn with an empty original render is a structure miss, and
    a turn with no fallback reports ``released_fallback`` false."""
    turn = _turn(_beat("say", "I can't say more.", 1), fallback=True)

    async def outcome(originals: dict[int, str], result: Any) -> Outcome:
        async def play(*_: Any, **__: Any) -> Any:
            return result

        async def originals_of(*_: Any) -> dict[int, str]:
            return originals

        async def kinds(*_: Any) -> list[str]:
            return ["do_not_assert:unsupported"]

        monkeypatch.setattr(e06, "play", play)
        monkeypatch.setattr(e06, "render_originals", originals_of)
        monkeypatch.setattr(e06, "rejected_kinds", kinds)
        ctx: Any = SimpleNamespace(deterministic=False)
        return await E06Suite().evaluate(ctx, _case(repeat=False))

    kept = await outcome({1: "The lamp was lit at dusk."}, turn)
    assert kept.observations[CONFORMANCE] is True
    assert kept.observations[e06.STRUCTURE] is True
    assert kept.observations[RELEASED_FALLBACK] is True
    assert kept.detail["fallback_code"] == "fallback:repair:do_not_assert:unsupported"
    assert kept.detail["structure_code"] is None
    assert kept.detail["released_problems"] == ["the safe fallback replaced the render"]
    empty = await outcome({1: ""}, turn)
    assert empty.observations[CONFORMANCE] is False
    assert empty.detail["structure_code"] == "a beat without text"
    clean = await outcome({}, _turn(_beat("say", "Welcome.", 1)))
    assert clean.observations[RELEASED_FALLBACK] is False
    assert clean.detail["fallback_code"] is None


def _line(observations: Mapping[str, bool | None], **detail: Any) -> dict[str, Any]:
    return {"suite": "e06", "observations": observations, "detail": detail}


@pytest.mark.part("M11", "NFR-QUAL-7")
def test_e_06__m11_tally() -> None:
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
        _line(
            {CONFORMANCE: True, "e06.structure_valid": True, RELEASED_FALLBACK: True},
            fallback_code="fallback:repair:do_not_assert:unsupported",
        ),
        {"suite": "e12", "observations": {}, "detail": {}},
    ]
    result = tally(lines)
    assert result["outcomes"] == 6
    assert result["parts"]["structure"] == (3, 6)
    assert result["parts"]["length"] == (1, 5)
    assert result["parts"]["repeat"] == (1, 1)
    assert result["codes"] == {
        "fallback:repair:uncertain": 1,
        "fallback:repair": 1,
        "model_error:model_unavailable:invalid_structure": 1,
    }
    assert code_of(lines[0]) is None
    assert code_of(lines[5]) is None
    assert result["fallbacks"] == {"fallback:repair:do_not_assert:unsupported": 1}
    assert "`fallback:repair` | 1 | 16.67% | yes" in render(result, None)
    assert "`fallback:repair:do_not_assert:unsupported` | 1 | 16.67% |" in render(result, None)


@pytest.mark.part("M11", "NFR-QUAL-7")
def test_e_06__m11_refusal_shapes() -> None:
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


@pytest.mark.part("M11", "NFR-QUAL-7")
def test_e_06__m11_clock_example() -> None:
    """#939's largest refusal shape on Full: with the world clock as the only tracked value, the
    effect syntax's example was a ``change`` on it, which the render refuses. The syntax now
    gives the time-advance line instead, and with another State listed the example is over
    that State and the clock line follows.

    Complement asserted: the example a ``gold`` State gets is still a ``change`` on it."""
    from epistrel.core.enums import StateType
    from epistrel.core.states.effects import CLOCK_LINE, syntax
    from epistrel.core.states.model import StateDefinition

    clock = StateDefinition(name="world_clock", type=StateType.CLOCK, unit="minutes")
    gold = StateDefinition(name="gold", type=StateType.NUMBER)
    refused = '"kind": "change", "state": "world_clock"'
    alone = syntax([clock], character=True)
    assert alone == CLOCK_LINE
    assert refused not in alone
    both = syntax([clock, gold], character=True)
    assert refused not in both
    assert '{"kind": "change", "state": "gold", "delta": -1' in both
    assert both.endswith(CLOCK_LINE)
    assert CLOCK_LINE not in syntax([gold], character=True)


async def _render_user_lines(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """E-06's stub run of one case; the user messages its renders were sent."""
    from epistrel.evals import SUITES
    from epistrel.orchestrator.models import stub as stub_mod
    from tests.eval.suite_support import SETS, stub_run

    seen: list[str] = []
    original = stub_mod.StubAdapter.complete

    async def spy(self: Any, purpose: str, messages: Any, *args: Any, **kwargs: Any) -> Any:
        schema = args[1] if len(args) > 1 else kwargs.get("response_schema")
        if purpose == "generation" and schema is not None and schema.__name__.endswith("Render"):
            seen.extend(m.content for m in messages if m.role == "user")
        return await original(self, purpose, messages, *args, **kwargs)

    monkeypatch.setattr(stub_mod.StubAdapter, "complete", spy)
    suite = SUITES["e06"]
    cases = [c for c in suite.load(SETS) if c.id == "generation_conformance-length-medium-0142"]
    await stub_run(pg_url, engine, suite, cases=cases)
    return seen


@pytest.mark.part("M11", "NFR-QUAL-7")
@pytest.mark.integration
async def test_e_06__m11_render_hears_the_player(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#939: E-06's story places Wren and Maren together, so Maren's render is sent the
    player's line (``Wren: "Who else is staying at the inn?"``); before the fix every E-06
    and E-07 render got the bare ``Wren:``.

    Complement asserted: with the placement skipped, the render gets the bare ``Wren:`` (the
    run's preset routes perception, and an unplaced character perceives nothing)."""
    from epistrel.evals.suites import structured_turns

    heard = await _render_user_lines(pg_url, engine, monkeypatch)
    assert heard
    assert all("Who else is staying at the inn?" in line for line in heard), heard

    async def unplaced(*_: Any) -> str:
        return ""

    monkeypatch.setattr(structured_turns, "place_cast", unplaced)
    deaf = await _render_user_lines(pg_url, engine, monkeypatch)
    assert deaf
    assert all(line.strip() == "Wren:" for line in deaf), deaf


@pytest.mark.part("M11", "NFR-QUAL-7")
def test_e_06__m11_tally_failures_file() -> None:
    """An ``evals run``'s failures file holds misses only: with ``total`` the shares are over
    the run's outcomes, and the repeat part has no denominator."""
    miss = {CONFORMANCE: False, "e06.structure_valid": False, "e06.length_honoured": True}
    result = tally([_line(miss, structure_code="fallback:repair")] * 3, total=600)
    assert result["outcomes"] == 600
    assert result["parts"]["structure"] == (3, 600)
    assert result["parts"]["repeat"] == (0, 0)
    assert "`fallback:repair` | 3 | 0.50% | yes" in render(result, None)


@pytest.mark.part("M11", "NFR-QUAL-7")
def test_e_06__m11_constraint_class() -> None:
    """A rejected render's constraint is coded by the ladder's wording class, never its text:
    an unpermitted claim keeps only its denial reason."""
    from epistrel.evals.suites.e06_conformance import constraint_class

    secret = "Do not introduce facts about Tomas: it is not this speaker's to tell (not in canon)."
    assert constraint_class("do_not_assert", secret) == "do_not_assert:unpermitted(not_in_canon)"
    assert constraint_class("do_not_assert", "Do not assert that Tomas is a smith.") == (
        "do_not_assert:contradicted_or_undecided"
    )
    assert constraint_class("do_not_assert", "Gold cannot go below 0.") == "do_not_assert:state"
    assert constraint_class("policy", "anything") == "policy"
    assert "Tomas" not in constraint_class("do_not_assert", secret)
