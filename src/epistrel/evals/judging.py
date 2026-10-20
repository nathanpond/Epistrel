"""The quality judge's calls and its calibration (NFR-QUAL-14,15; E-12, E-13; #158).

**Masking.** Before a pair is judged, ``mask`` replaces the story's character names with
``[CHARACTER_n]``, its place and thing names with ``[PLACE_n]``, dates with ``[DATE]`` and
quantities with ``[N]``, the same way in both passages, so the judge compares voice and
readability, not the facts a repair changed.

**A pairwise judgement** (``compare``) asks ``quality_judge`` twice, with the passages in both
orders (``a`` then ``b``, ``b`` then ``a``), each call in its own eval scope
(``<case>.ab``/``<case>.ba``, so a stub run scripts each order: ``script_pair``). The verdict is
the one both orders give; a disagreement between them is a ``tie``, and an explicit ``tie``
counts as tied. An ``Uncertain`` answer (garbage after #53's retries) excludes the pair: its
preference is ``None``. Identical passages are a tie without a call (FR-MODEL-5). **A score**
(``score``) asks the ``score`` variant once: 1 to 5, or ``None`` for an ``Uncertain`` answer.

Every judge call runs at temperature 0 through ``structured_call`` with its own ``TurnBudget``
(a 300 s deadline): never a turn's budget, never inside a turn. It counts in
``epistrel_model_calls_total{purpose="quality_judge"}`` as every call does. Nothing it reads or
answers is written to any story.

**Calibration** (``calibrate``). Before any judge-based result is certified, the judge is run on
the labeled set ``evals/sets/judge_calibration/cases.jsonl`` (at least ``CALIBRATION_MIN_PAIRS``
human-labeled pairs: the builder labels, the owner approves). Its **agreement** is the share of
pairs whose combined verdict matches the label exactly (``a``, ``b`` or ``tie``); an excluded
pair (an ``Uncertain`` answer) counts as a disagreement, never an exclusion. It is reported with
its Wilson 95 % interval. The judge is ``calibrated`` when it covered at least the minimum pairs
and its agreement is at least ``CALIBRATION_FLOOR`` (75 %), else ``uncalibrated``: every
judge-based row of a run under it is ``report_only``.

The result is stored by judge model in ``judge-calibration.json`` (``docs/evals/`` for a real
model, committed beside its reports; ``build/evals/`` for the stub). A run reads the entry for
its judge model; a judge model change is a new key, so an older calibration never vouches for a
newer judge, and earlier reports keep the judge they recorded.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.evals.metrics import wilson
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import EVAL_SCOPE, ModelAccessLayer, eval_scope
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.registry import (
    QUALITY_JUDGE,
    JudgeCriteria,
    JudgePair,
    QualityJudgeInputs,
    QualityJudgeScore,
    QualityJudgeVerdict,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.models.stub import StubAdapter

CALIBRATION_FLOOR = 0.75
CALIBRATION_MIN_PAIRS = 200
CALIBRATION_SET = "judge_calibration/cases.jsonl"
CALIBRATION_FILE = "judge-calibration.json"
JUDGE_DEADLINE_S = 300.0
JUDGE_PARAMS = ModelParams(temperature=0.0)
SCORE_VARIANT = "score"

type Preference = Literal["a", "b", "tie"]
type Status = Literal["calibrated", "uncalibrated", "not_run"]

_SWAP: Mapping[str, Preference] = {"a": "b", "b": "a", "tie": "tie"}


# -- masking ----------------------------------------------------------------------------------

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE = re.compile(
    rf"\b(?:(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*\d{{4}})?"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})(?:\s+\d{{4}})?"
    r"|\d{4}-\d{2}-\d{2}|(?:1[5-9]|20)\d{2})\b"
)
_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    "sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    "hundred|thousand|dozen"
)
_NUMBER = re.compile(rf"\b(?:\d+(?:[.,]\d+)?|(?:{_NUMBER_WORDS})(?:[- ](?:{_NUMBER_WORDS}))*)\b")


def _name_pattern(name: str) -> re.Pattern[str] | None:
    words = name.split()
    if not words:
        return None
    return re.compile(r"(?<![\w'])" + r"\s+".join(re.escape(w) for w in words) + r"(?![\w'])")


def mask(text: str, characters: Sequence[str] = (), places: Sequence[str] = ()) -> str:
    """``text`` with ``characters`` → ``[CHARACTER_n]``, ``places`` → ``[PLACE_n]`` (``n`` by
    position in the list given, longest names first so a full name wins over its first word),
    dates → ``[DATE]`` and quantities → ``[N]``. Deterministic: the same lists mask both
    passages of a pair alike."""
    labelled = [(name, f"[CHARACTER_{i}]") for i, name in enumerate(characters, start=1)]
    labelled += [(name, f"[PLACE_{i}]") for i, name in enumerate(places, start=1)]
    out = text
    for name, label in sorted(labelled, key=lambda item: -len(item[0])):
        pattern = _name_pattern(name)
        if pattern is not None:
            out = pattern.sub(label, out)
    out = _DATE.sub("[DATE]", out)
    return _NUMBER.sub("[N]", out)


def normalized(text: str) -> str:
    """``text`` with its white space collapsed: E-12's "changed" is inequality of this."""
    return " ".join(text.split())


# -- the calls --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PairJudgement:
    """One pair judged in both orders: the combined ``preferred`` (``None``: excluded) and each
    order's own verdict in the pair's frame (``a`` is always the first passage given)."""

    preferred: Preference | None
    forward: Preference | None
    backward: Preference | None
    reasons: tuple[str, ...] = ()


def _scope_id(case_id: str, suffix: str) -> str:
    return f"{case_id}.{suffix}"


def _seed() -> int | None:
    scope = EVAL_SCOPE.get()
    return None if scope is None else scope.seed


async def _verdict(
    models: ModelAccessLayer, first: str, second: str, criteria: JudgeCriteria, scope: str
) -> QualityJudgeVerdict | Uncertain:
    inputs = QualityJudgeInputs(pair=JudgePair(a=first, b=second), criteria=criteria)

    def identical() -> QualityJudgeVerdict | None:
        # FR-MODEL-5's deterministic step: the same passage twice is a tie, no call needed.
        same = normalized(first) == normalized(second)
        return QualityJudgeVerdict(preferred="tie", reasons="identical") if same else None

    with eval_scope(scope, _seed()):
        return await models.structured_call(
            QUALITY_JUDGE.name,
            QualityJudgeVerdict,
            inputs,
            params=JUDGE_PARAMS,
            deterministic_first=identical,
            budget=TurnBudget(deadline_s=JUDGE_DEADLINE_S),
        )


def _no_rule() -> QualityJudgeScore | None:
    """A score has no deterministic answer (FR-MODEL-5's step, declared and empty)."""
    return None


def combine(forward: Preference | None, backward: Preference | None) -> Preference | None:
    """Both orders' verdicts (``backward`` already in the pair's frame) → the pair's: ``None``
    when either is missing, the shared verdict when they agree, else ``tie``."""
    if forward is None or backward is None:
        return None
    return forward if forward == backward else "tie"


async def compare(
    models: ModelAccessLayer,
    a: str,
    b: str,
    criteria: JudgeCriteria = "voice_readability",
    *,
    case_id: str,
) -> PairJudgement:
    """``a`` against ``b``, judged in both orders (module docstring)."""
    if normalized(a) == normalized(b):
        return PairJudgement("tie", "tie", "tie")
    first = await _verdict(models, a, b, criteria, _scope_id(case_id, "ab"))
    second = await _verdict(models, b, a, criteria, _scope_id(case_id, "ba"))
    forward = None if isinstance(first, Uncertain) else first.preferred
    backward = None if isinstance(second, Uncertain) else _SWAP[second.preferred]
    reasons = tuple(
        v.reasons for v in (first, second) if isinstance(v, QualityJudgeVerdict) and v.reasons
    )
    return PairJudgement(combine(forward, backward), forward, backward, reasons)


async def score(
    models: ModelAccessLayer,
    message: str,
    reference: str,
    criteria: JudgeCriteria,
    *,
    case_id: str,
) -> int | None:
    """``message`` scored 1-5 against ``reference`` on ``criteria``; ``None`` when the judge's
    answer was ``Uncertain``."""
    inputs = QualityJudgeInputs(message=message, reference=reference or None, criteria=criteria)
    with eval_scope(_scope_id(case_id, "score"), _seed()):
        found = await models.structured_call(
            QUALITY_JUDGE.name,
            QualityJudgeScore,
            inputs,
            params=JUDGE_PARAMS,
            deterministic_first=_no_rule,
            budget=TurnBudget(deadline_s=JUDGE_DEADLINE_S),
            variant=SCORE_VARIANT,
        )
    return None if isinstance(found, Uncertain) else found.score


# -- stub scripting ---------------------------------------------------------------------------


def verdict_json(preferred: Preference, reasons: str = "scripted") -> str:
    return json.dumps({"preferred": preferred, "reasons": reasons})


def script_pair(stub: StubAdapter, case_id: str, preferred: Preference) -> None:
    """Script the stub's judge to prefer ``preferred`` (in the pair's frame) in both orders of
    the pair judged under ``case_id``."""
    purpose = QUALITY_JUDGE.name
    stub.case(_scope_id(case_id, "ab"), verdict_json(preferred), purpose=purpose)
    stub.case(_scope_id(case_id, "ba"), verdict_json(_SWAP[preferred]), purpose=purpose)


def script_score(stub: StubAdapter, case_id: str, value: int) -> None:
    """Script the stub's judge to score the message judged under ``case_id`` ``value``."""
    stub.case(
        _scope_id(case_id, "score"),
        json.dumps({"score": value, "reasons": "scripted"}),
        purpose=QUALITY_JUDGE.name,
    )


# -- calibration ------------------------------------------------------------------------------


class CalibrationPair(BaseModel):
    """One labeled pair of ``judge_calibration/cases.jsonl``: the passages ``a`` and ``b`` (shown
    in that order first), what they are compared on, the human label, who labeled it and where
    the pair comes from (``source``: the construction it follows)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    a: str = Field(min_length=1, max_length=4_000)
    b: str = Field(min_length=1, max_length=4_000)
    criteria: JudgeCriteria = "voice_readability"
    names: list[str] = Field(default_factory=list, max_length=20)
    """The character names in the passages: masked before judging, as E-12 masks its pairs."""
    label: Preference
    labeled_by: Literal["builder", "owner"]
    source: str = Field(min_length=1, max_length=200)


def load_pairs(sets_dir: Path) -> list[CalibrationPair]:
    """The labeled calibration pairs under ``sets_dir`` (``MalformedCase`` naming a bad line)."""
    from epistrel.evals.suites import MalformedCase  # the suites import this module

    path = sets_dir / CALIBRATION_SET
    if not path.is_file():
        raise MalformedCase(f"judge calibration: no labeled set at {path}")
    found: list[CalibrationPair] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            found.append(CalibrationPair.model_validate_json(line))
        except ValueError as exc:
            raise MalformedCase(f"judge calibration: line {number}: {exc}") from None
    ids = [p.id for p in found]
    if len(set(ids)) != len(ids):
        raise MalformedCase("judge calibration: duplicate pair ids")
    return found


class LabelTally(BaseModel):
    model_config = ConfigDict(frozen=True)

    pairs: int
    agreed: int


class Calibration(BaseModel):
    """One judge model's calibration (module docstring)."""

    model_config = ConfigDict(frozen=True)

    judge_model: str
    model_version: str | None = None
    date: str
    engine_sha: str = ""
    pairs: int
    agreed: int
    excluded: int
    agreement: float | None
    ci95: list[float] | None
    floor: float = CALIBRATION_FLOOR
    min_pairs: int = CALIBRATION_MIN_PAIRS
    status: Literal["calibrated", "uncalibrated"]
    by_label: dict[str, LabelTally] = Field(default_factory=dict)


def calibration_status(pairs: int, agreed: int) -> Literal["calibrated", "uncalibrated"]:
    """``calibrated`` on at least ``CALIBRATION_MIN_PAIRS`` pairs with agreement at least
    ``CALIBRATION_FLOOR``."""
    if pairs >= CALIBRATION_MIN_PAIRS and agreed / pairs >= CALIBRATION_FLOOR:
        return "calibrated"
    return "uncalibrated"


def summarize(
    results: Iterable[tuple[CalibrationPair, Preference | None]],
    *,
    judge_model: str,
    model_version: str | None = None,
    engine_sha: str = "",
    when: datetime | None = None,
) -> Calibration:
    """The calibration of ``judge_model`` from each labeled pair and the judge's combined
    verdict (``None``: excluded, counted as a disagreement)."""
    pairs = agreed = excluded = 0
    by_label: dict[str, list[int]] = {}
    for pair, preferred in results:
        pairs += 1
        hit = preferred == pair.label
        agreed += hit
        excluded += preferred is None
        tally = by_label.setdefault(pair.label, [0, 0])
        tally[0] += 1
        tally[1] += hit
    interval = wilson(agreed, pairs)
    return Calibration(
        judge_model=judge_model,
        model_version=model_version,
        date=(when or datetime.now(UTC)).date().isoformat(),
        engine_sha=engine_sha,
        pairs=pairs,
        agreed=agreed,
        excluded=excluded,
        agreement=agreed / pairs if pairs else None,
        ci95=list(interval) if interval is not None else None,
        status=calibration_status(pairs, agreed),
        by_label={k: LabelTally(pairs=v[0], agreed=v[1]) for k, v in sorted(by_label.items())},
    )


async def judge_pairs(
    models: ModelAccessLayer, pairs: Sequence[CalibrationPair], *, seed: int | None = None
) -> list[tuple[CalibrationPair, Preference | None]]:
    """The judge's combined verdict on each labeled pair (scoped ``calibration.<id>``)."""
    found: list[tuple[CalibrationPair, Preference | None]] = []
    for pair in pairs:
        case_id = f"calibration.{pair.id}"
        a, b = mask(pair.a, pair.names), mask(pair.b, pair.names)
        with eval_scope(case_id, seed):
            judged = await compare(models, a, b, pair.criteria, case_id=case_id)
        found.append((pair, judged.preferred))
    return found


def script_calibration(
    stub: StubAdapter, pairs: Sequence[CalibrationPair], flip: Collection[str] = ()
) -> None:
    """Script the stub's judge to answer every pair's label (100 % agreement), except the pairs
    named in ``flip``, answered with the opposite preference (a tie becomes ``a``)."""
    for pair in pairs:
        answer: Preference = pair.label
        if pair.id in flip:
            answer = "a" if pair.label == "tie" else _SWAP[pair.label]
        script_pair(stub, f"calibration.{pair.id}", answer)


# -- the store ----------------------------------------------------------------------------------


def load_store(path: Path) -> dict[str, Calibration]:
    """Every stored calibration by judge model (empty when the file does not exist)."""
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {model: Calibration.model_validate(entry) for model, entry in raw.items()}


def save(path: Path, calibration: Calibration) -> None:
    """Store ``calibration`` under its judge model, keeping the other models' entries."""
    stored = load_store(path)
    stored[calibration.judge_model] = calibration
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {k: v.model_dump(mode="json") for k, v in sorted(stored.items())}
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def lookup(path: Path, judge_model: str) -> Calibration | None:
    """The stored calibration of ``judge_model``, if any."""
    return load_store(path).get(judge_model)


__all__ = [
    "CALIBRATION_FILE",
    "CALIBRATION_FLOOR",
    "CALIBRATION_MIN_PAIRS",
    "CALIBRATION_SET",
    "Calibration",
    "CalibrationPair",
    "PairJudgement",
    "Preference",
    "Status",
    "calibration_status",
    "combine",
    "compare",
    "judge_pairs",
    "load_pairs",
    "load_store",
    "lookup",
    "mask",
    "normalized",
    "save",
    "score",
    "script_calibration",
    "script_pair",
    "script_score",
    "summarize",
    "verdict_json",
]
