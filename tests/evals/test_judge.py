"""The quality judge's calls and calibration (#158; ``epistrel.evals.judging``), without a
database: masking, the two orders and how they combine, the score variant, the calibration's
agreement, interval and status, and the store keyed by judge model."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from epistrel.evals.cli import main
from epistrel.evals.judging import (
    CALIBRATION_FLOOR,
    CALIBRATION_MIN_PAIRS,
    CalibrationPair,
    Preference,
    calibration_status,
    combine,
    compare,
    judge_pairs,
    load_pairs,
    lookup,
    mask,
    save,
    score,
    script_calibration,
    script_pair,
    script_score,
    summarize,
)
from epistrel.evals.metrics import wilson
from epistrel.orchestrator.models.access import ModelAccessLayer, eval_scope
from epistrel.orchestrator.models.registry import QUALITY_JUDGE
from tests.auth_support import auth_settings
from tests.eval.suite_support import SETS


def _layer() -> ModelAccessLayer:
    return ModelAccessLayer(
        auth_settings("postgresql+psycopg://u:p@localhost:5432/x", model_adapter="stub")
    )


def _pair(n: int, label: str = "a") -> CalibrationPair:
    return CalibrationPair.model_validate(
        {
            "id": f"p-{n:04d}",
            "family": "broken",
            "origin": "authored",
            "a": f"Bobby says {n}.",
            "b": "Bobby",
            "names": ["Bobby"],
            "label": label,
            "labeled_by": "builder",
            "source": "test",
        }
    )


def test_masking_is_the_same_in_both_passages() -> None:
    """Names (a full name before its first word), places, dates and quantities are masked
    alike, so a repair that changed a fact does not read as a difference of voice."""
    draft = "Bobby Hale hangs his coat in the hallway on March 3, 1987, with 40 silver."
    final = "Bobby hangs his coat on the hook in the hallway, with forty silver."
    names, places = ("Bobby Hale", "Bobby"), ("hallway", "hook")
    assert mask(draft, names, places) == (
        "[CHARACTER_1] hangs his coat in the [PLACE_1] on [DATE], with [N] silver."
    )
    assert mask(final, names, places) == (
        "[CHARACTER_2] hangs his coat on the [PLACE_2] in the [PLACE_1], with [N] silver."
    )
    assert mask("Bobbyson waits.", ("Bobby",)) == "Bobbyson waits."  # whole words only


def test_both_orders_and_their_combination() -> None:
    """The verdict is the one both orders give; a disagreement is a tie; a missing answer
    excludes the pair."""
    assert combine("a", "a") == "a"
    assert combine("b", "b") == "b"
    assert combine("a", "b") == "tie"
    assert combine("tie", "a") == "tie"
    assert combine(None, "a") is None
    assert combine("a", None) is None


async def test_compare_asks_twice_with_the_order_swapped() -> None:
    """``compare`` makes two ``quality_judge`` calls at temperature 0, the second with the
    passages swapped, each in its own scope (``<case>.ab``/``.ba``), and maps the second back to
    the pair's frame. Complements: identical passages are a tie with no call; an order whose
    answer is garbage (``Uncertain``) excludes the pair."""
    layer = _layer()
    stub = layer.stub
    script_pair(stub, "c1.t1", "b")
    with eval_scope("c1", 7):
        judged = await compare(layer, "First draft.", "Final message.", case_id="c1.t1")
    assert (judged.preferred, judged.forward, judged.backward) == ("b", "b", "b")
    first, second = stub.calls
    assert {first.purpose, second.purpose} == {QUALITY_JUDGE.name}
    assert first.params.temperature == 0.0
    assert first.params.seed == 7
    shown = next(m.content for m in first.messages if m.role == "user")
    assert shown.index("First draft.") < shown.index("Final message.")
    swapped = next(m.content for m in second.messages if m.role == "user")
    assert swapped.index("Final message.") < swapped.index("First draft.")
    assert "X-Eval-Case: c1.t1.ab" in first.messages[0].content
    assert "X-Eval-Case: c1.t1.ba" in second.messages[0].content

    calls = len(stub.calls)
    same = await compare(layer, "Same  text.", "Same text.", case_id="c1.t2")
    assert same.preferred == "tie"
    assert len(stub.calls) == calls

    stub.case("c1.t3.ab", json.dumps({"preferred": "a"}), purpose=QUALITY_JUDGE.name)
    stub.fault(QUALITY_JUDGE.name, "garbage", n=10)
    excluded = await compare(layer, "One.", "Two.", case_id="c1.t3")
    assert excluded.preferred is None


async def test_score_uses_its_variant() -> None:
    layer = _layer()
    script_score(layer.stub, "c2.voice", 4)
    with eval_scope("c2"):
        found = await score(
            layer, "Me? Lazy?", "Bobby is wry.", "voice_fits_definition", case_id="c2.voice"
        )
    assert found == 4
    (call,) = layer.stub.calls
    assert call.response_schema == "QualityJudgeScore"
    assert "## Reference" in call.messages[-1].content


def test_calibration_agreement_interval_and_status() -> None:
    """Agreement is exact matches over every pair, an excluded pair counting as a
    disagreement; the interval is Wilson's; at least 200 pairs at 75 % or more is calibrated.
    Complements: 74.5 % is not, nor is a perfect judge on 199 pairs."""
    pairs = [_pair(n) for n in range(200)]
    a: list[Preference | None] = ["a"] * 150
    b: list[Preference | None] = ["b"] * 49
    answers = [*a, *b, None]
    found = summarize(
        zip(pairs, answers, strict=True), judge_model="m1", when=datetime(2026, 10, 20, tzinfo=UTC)
    )
    assert (found.pairs, found.agreed, found.excluded) == (200, 150, 1)
    assert found.agreement == 0.75
    assert found.ci95 == pytest.approx(list(wilson(150, 200) or ()))
    assert found.status == "calibrated"
    assert found.date == "2026-10-20"
    assert calibration_status(200, 149) == "uncalibrated"
    assert calibration_status(CALIBRATION_MIN_PAIRS - 1, CALIBRATION_MIN_PAIRS - 1) == (
        "uncalibrated"
    )
    assert CALIBRATION_FLOOR == 0.75


def test_the_store_is_keyed_by_judge_model(tmp_path: Path) -> None:
    """A calibration is stored per judge model: another model has none, and an earlier
    model's entry stays."""
    path = tmp_path / "judge-calibration.json"
    first = summarize([(_pair(1), "a")], judge_model="model-a")
    save(path, first)
    save(path, summarize([(_pair(1), "b")], judge_model="model-b"))
    assert lookup(path, "model-a") == first
    assert lookup(path, "model-b") is not None
    assert lookup(path, "model-c") is None


async def test_the_labeled_set_and_the_stub_calibration() -> None:
    """The committed set holds at least 200 builder-labeled pairs, all three labels among them;
    the stub scripted to every label agrees on every pair, and a stub that flips 30 % of them
    falls below the floor."""
    pairs = load_pairs(SETS)
    assert len(pairs) >= CALIBRATION_MIN_PAIRS
    assert {p.label for p in pairs} == {"a", "b", "tie"}
    assert {p.labeled_by for p in pairs} == {"builder"}
    layer = _layer()
    script_calibration(layer.stub, pairs[:40])
    perfect = summarize(await judge_pairs(layer, pairs[:40]), judge_model="stub")
    assert perfect.agreement == 1.0
    layer = _layer()
    flipped = {p.id for p in pairs[:12]}
    script_calibration(layer.stub, pairs[:40], flipped)
    weak = summarize(await judge_pairs(layer, pairs[:40]), judge_model="stub")
    assert weak.agreement == pytest.approx(28 / 40)
    assert weak.status == "uncalibrated"


def test_calibrate_judge_needs_an_endpoint(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["calibrate-judge", "--adapter", "openai"]) == 2
    assert "--base-url and --model" in capsys.readouterr().err
