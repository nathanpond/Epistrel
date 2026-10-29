"""E-08 M5 (docs/04 §8; NFR-QUAL-1,8; #179): NFR-QUAL-8's gate rules applied to NFR-QUAL-1's
knowledge-asymmetry and private-thought metrics on their labeled sets, through the real suites
(``q04``, ``priv_think_leak``) on the stub, not the synthetic one (#134's M4 part,
``tests/evals/test_harness.py::test_e_08__m4_gate_rules``).

The runs use the stub at four cases at once (each case is one or two structured turns).
``test_e_08__m5_think_leak`` plays the private-thought suite's full set on two seeds (its 345
measured cases: the clean pass). ``test_e_08__m5_real_suites`` plays the asymmetry suite on 299
of its cases (one seed: the short run) and on 301 with E-08's injected violation and one model
error (one seed: the violation), and E-01 M5's viewpoint suite beside them (the rate rule). The
two are separate functions so CI can run them in separate shards (#179).
``test_e_08__m5_model_errors`` plays small slices of both zero-leak suites with model errors
injected (#179's Rule 1 fix): an error is reported on the suite's model-error row and is never
scored as a disclosure. ``test_e_08__m5_think_leak_every_form`` and ``__injected`` (#562): the
private-thought oracle checks every case against every category's forms, so a paraphrased or
narrated disclosure in a case of another category is seen (by the oracle alone, and end to end
on the stub). ``test_e_08__m5_think_leak_reworded`` and ``__reworded_neutral`` (#575): the
oracle sees reworded disclosures of the fact, feeling and thought by lemma and stem matching,
leaves neutral lines clean, and detects every control of each kind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, Rule, Verdict, fixtures, run, upper_bound_95
from epistrel.evals.authoring.asymmetry import render
from epistrel.evals.report import MetricResult, Report, render_markdown
from epistrel.evals.runner import ERROR_DETAILS
from epistrel.evals.suites import priv_think_leak as think_leak
from epistrel.evals.suites import q04_asymmetry as q04
from epistrel.evals.suites.priv_think_leak import THINK_LEAK, ThinkLeakCase, case_line
from epistrel.evals.suites.q04_asymmetry import ASYMMETRY, INJECTED_CASE, AsymmetryCase, Q04Suite
from epistrel.orchestrator.models.registry import GENERATION
from tests.eval.suite_support import SEEDS, SETS
from tests.evals.support import stub_engine

pytestmark = pytest.mark.integration

CONCURRENCY = 4
BROKEN = "this is not a render"
"""A generation answer that never validates: the turn fails with ``invalid_structure``."""


def _metric(report: Report, metric_id: str) -> MetricResult:
    (suite,) = report.suites
    return next(m for m in suite.metrics if m.id == metric_id)


def _reported(report: Report, metric: MetricResult, n: int, seeds: int) -> None:
    """Every report carries its sample size, seed count, model and version (and the zero rule's
    bound when clean)."""
    assert report.harness_errors == []
    assert (report.model, report.model_version, report.seed_count) == ("stub", "stub", seeds)
    assert (metric.sample_size, metric.seed_count, metric.rule) == (n, seeds, Rule.zero)


@pytest.mark.part("M5", "NFR-QUAL-1,8")
async def test_e_08__m5_think_leak(pg_url: str, engine: AsyncEngine) -> None:
    """On the real private-thought suite: its 345 measured cases (3 x 115, sized past the
    300-case floor for model errors, #179), clean on both seeds, pass, reported "0 observed in
    345 cases" with a one-sided 95% upper bound under 1% (never as "leak-free").

    Complement asserted: the clean run's controls are all detected (the oracle is not blind)."""
    think = SUITES["priv_think_leak"]
    async with stub_engine(pg_url, engine) as lib:
        clean = await run(think, AdapterConfig(), SEEDS, engine=lib, concurrency=CONCURRENCY)
    measured = sum(
        not c.control for c in think.load(SETS) if isinstance(c, ThinkLeakCase)
    )  # fmt: skip
    assert measured == 345
    metric = _metric(clean, THINK_LEAK)
    _reported(clean, metric, 345, 2)
    assert (metric.violations, metric.verdict) == (0, Verdict.passed)
    assert metric.upper_bound_95 == pytest.approx(upper_bound_95(345))
    assert metric.upper_bound_95 == pytest.approx(0.0086, abs=0.0005)
    assert _metric(clean, "priv_think_leak.control_detected").mean == 1.0
    assert clean.suites[0].verdict is Verdict.passed
    markdown = render_markdown(clean)
    assert "0 observed in 345 cases, one-sided 95% upper bound ≈ 0.86%" in markdown
    assert "leak-free" not in markdown.lower()
    # #181: the zero-leak suites run under the seeded profile itself (no dials), so the suite
    # reports the run's own fingerprint.
    assert clean.suites[0].fingerprint is None
    assert clean.suites[0].profile is None


@pytest.mark.part("M5", "NFR-QUAL-1,8")
async def test_e_08__m5_real_suites(pg_url: str, engine: AsyncEngine) -> None:
    """On the real asymmetry suite: 299 clean cases fail (fewer than 300); 300 cases with one
    deterministically injected violation (the scripted stub leaks on ``asymmetry-family-0007``
    on one seed, a paraphrase the pipeline cannot catch) and one more case ended by a model
    error fail, with the violation counted once (the error is not one, #179) and no bound. The
    viewpoint suite passes its rate floors beside them.

    Complement asserted: the 299 and the 300 runs differ only in the one case and the leak."""
    asymmetry = SUITES["q04"]
    assert isinstance(asymmetry, Q04Suite)
    pool = sorted(
        (c for c in asymmetry.load(SETS) if isinstance(c, AsymmetryCase) and not c.control),
        key=lambda c: c.id,
    )
    injected = next(c for c in pool if c.id == INJECTED_CASE)
    others = [c for c in pool if c.id != INJECTED_CASE][:299]
    errored = next(c for c in pool if c.id != INJECTED_CASE and c not in others)
    one_seed = SEEDS[:1]
    async with stub_engine(pg_url, engine) as lib:
        short = await run(
            asymmetry,
            AdapterConfig(),
            one_seed,
            engine=lib,
            cases={asymmetry.name: others},
            concurrency=CONCURRENCY,
        )
    short_metric = _metric(short, ASYMMETRY)
    _reported(short, short_metric, 299, 1)
    assert (short_metric.violations, short_metric.verdict) == (0, Verdict.failed)

    async with stub_engine(pg_url, engine) as lib:
        asymmetry.inject(lib.models.stub, injected, one_seed[0])
        lib.models.stub.case(errored.id, BROKEN, purpose=GENERATION.name, seed=one_seed[0])
        leaked = await run(
            asymmetry,
            AdapterConfig(),
            one_seed,
            engine=lib,
            cases={asymmetry.name: [*others, injected, errored]},
            concurrency=CONCURRENCY,
        )
    leak = _metric(leaked, ASYMMETRY)
    _reported(leaked, leak, 300, 1)
    assert (leak.violations, leak.verdict, leak.upper_bound_95) == (1, Verdict.failed, None)
    assert leak.per_seed == [pytest.approx(1 / 300)]
    lost = _metric(leaked, q04.MODEL_ERROR)
    assert (lost.sample_size, lost.verdict) == (301, Verdict.report_only)
    assert lost.per_seed == [pytest.approx(1 / 301)]

    viewpoint = SUITES["e01_viewpoint"]
    async with stub_engine(pg_url, engine) as lib:
        rated = await run(viewpoint, AdapterConfig(), SEEDS, engine=lib)
    assert rated.suites[0].verdict is Verdict.passed


def _failures(path: Path) -> dict[tuple[str, int], dict[str, object]]:
    lines = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return {(x["case"]["id"], x["seed"]): x for x in lines}


def _assert_model_error(record: dict[str, object], row: str) -> None:
    """A model error's failure record: its row bad and nothing else observed, no output, and
    the error's code, reason and structural codes in ``detail`` (never story content)."""
    observations = record["observations"]
    assert isinstance(observations, dict)
    assert observations.pop(row) is False
    assert set(observations.values()) == {None}
    assert record["output"] == ""
    detail = record["detail"]
    assert isinstance(detail, dict)
    assert set(detail) <= {"model_error", "error_type", *ERROR_DETAILS}
    assert (detail["model_error"], detail["purpose"], detail["reason"]) == (
        "model_unavailable",
        GENERATION.name,
        "invalid_structure",
    )
    codes = detail["structural_errors"]
    assert isinstance(codes, list)
    assert codes
    assert all(isinstance(c, str) and BROKEN not in c for c in codes)


@pytest.mark.part("M5", "NFR-QUAL-1,8")
async def test_e_08__m5_model_errors(pg_url: str, engine: AsyncEngine, tmp_path: Path) -> None:
    """#179's Rule 1 fix (the owner's live run scored 413 model errors as disclosures): on a
    Q-04 slice of four withheld cases and two controls, two seeds, a model error injected on one
    withheld case and one control (an answer that never validates: ``invalid_structure``)
    scores neither as a disclosure, a missed control or a repair. ``q04.model_error`` reports
    both (report-only), the zero rule counts only the completed cases, and the suite's verdict
    is decided by them (a slice this short fails by the observed-zero rule, as a clean one
    does). Each error's failure record names the error's reason and structural codes, with no
    output. The private-thought suite alike: an errored withheld case observes no category row.

    Complement asserted: the same slice played clean has the same verdicts, every case observed,
    no failure record and a ``model_error`` row of 0."""
    suite = SUITES["q04"]
    cases = [c for c in suite.load(SETS) if isinstance(c, AsymmetryCase)]
    withheld = [c for c in cases if not c.control][:4]
    controls = [c for c in cases if c.control][:2]
    picked = {suite.name: [*withheld, *controls]}
    clean_log, broken_log = tmp_path / "clean.jsonl", tmp_path / "broken.jsonl"
    async with stub_engine(pg_url, engine) as lib:
        clean = await run(
            suite, AdapterConfig(), SEEDS, engine=lib, cases=picked, failures=clean_log
        )
    async with stub_engine(pg_url, engine) as lib:
        for case in (withheld[0], controls[0]):
            for seed in SEEDS:
                lib.models.stub.case(case.id, BROKEN, purpose=GENERATION.name, seed=seed)
        broken = await run(
            suite, AdapterConfig(), SEEDS, engine=lib, cases=picked, failures=broken_log
        )

    assert clean.harness_errors == []
    assert broken.harness_errors == []
    expected = {  # metric: (clean (n, mean), errored (n, mean)) per seed
        ASYMMETRY: ((4, 0.0), (3, 0.0)),
        q04.CONTROL: ((2, 1.0), (1, 1.0)),
        q04.REPAIRED: ((6, 0.0), (4, 0.0)),
        q04.MODEL_ERROR: ((6, 0.0), (6, 2 / 6)),
    }
    for metric_id, (calm, hit) in expected.items():
        for report, (n, mean) in ((clean, calm), (broken, hit)):
            found = _metric(report, metric_id)
            assert (found.sample_size, found.seed_count) == (n, 2), metric_id
            assert found.mean == pytest.approx(mean), metric_id
    assert _metric(broken, ASYMMETRY).violations == 0
    assert _metric(broken, q04.MODEL_ERROR).verdict is Verdict.report_only
    # Four completed cases are far short of 300 with or without the errors: failed, both runs.
    assert _metric(clean, ASYMMETRY).verdict is Verdict.failed
    assert _metric(broken, ASYMMETRY).verdict is Verdict.failed
    assert clean.suites[0].verdict is broken.suites[0].verdict is Verdict.failed
    assert _failures(clean_log) == {}
    records = _failures(broken_log)
    assert set(records) == {(c.id, seed) for c in (withheld[0], controls[0]) for seed in SEEDS}
    for record in records.values():
        _assert_model_error(record, q04.MODEL_ERROR)

    leak_suite = SUITES["priv_think_leak"]
    pool = [c for c in leak_suite.load(SETS) if isinstance(c, ThinkLeakCase)]
    measured = [c for c in pool if not c.control]
    failing = measured[0]
    passing = next(c for c in measured[1:] if c.inputs.category == failing.inputs.category)
    control = next(c for c in pool if c.control)
    one_seed = SEEDS[:1]
    async with stub_engine(pg_url, engine) as lib:
        for thinking in (failing, control):
            lib.models.stub.when((case_line(thinking),), BROKEN, GENERATION.name)
        thought = await run(
            leak_suite,
            AdapterConfig(),
            one_seed,
            engine=lib,
            cases={leak_suite.name: [failing, passing, control]},
            failures=broken_log,
        )
    assert thought.harness_errors == []
    category = think_leak.CATEGORY_METRICS[failing.inputs.category]
    rows: list[tuple[str, int, float | None]] = [
        (THINK_LEAK, 1, 0.0),
        (category, 1, 0.0),
        (think_leak.CONTROL, 0, None),
        (think_leak.MODEL_ERROR, 3, 2 / 3),
    ]
    for metric_id, n, want in rows:
        found = _metric(thought, metric_id)
        assert found.sample_size == n, metric_id
        assert found.mean == (None if want is None else pytest.approx(want)), metric_id
    assert _metric(thought, THINK_LEAK).violations == 0
    leaked = {k: v for k, v in _failures(broken_log).items() if v["suite"] == leak_suite.name}
    assert set(leaked) == {(failing.id, one_seed[0]), (control.id, one_seed[0])}
    for record in leaked.values():
        _assert_model_error(record, think_leak.MODEL_ERROR)


# -- #562: every case is checked against every category's forms -----------------------------

KINDS = ("verbatim", "paraphrase", "narrated")


def _disclosure(case: ThinkLeakCase, kind: str) -> str:
    """A ``say`` disclosing ``case``'s thought in ``kind``'s form: the thought's own words, the
    fact's paraphrased ``leak`` (one of its forms, past the pipeline's own detector) or one of
    its narrated feelings."""
    fact = fixtures.FIXTURES[case.inputs.fixture_id].fact(case.inputs.fact)
    if kind == "verbatim":
        return fact.think
    if kind == "paraphrase":
        assert fixtures.disclosures(fact.leak, fact.forms), fact.key
        return fact.leak
    return f"{fact.holder} seems {fact.feelings[0]}, if you ask me."


def _injections() -> list[tuple[ThinkLeakCase, str, str]]:
    """A distinct withheld case per (category, other kind): its measured answer discloses the
    thought in another category's form, which the case's own forms do not hold."""
    found: list[tuple[ThinkLeakCase, str, str]] = []
    pool = [
        c for c in SUITES["priv_think_leak"].load(SETS) if isinstance(c, ThinkLeakCase)
    ]  # fmt: skip
    for category in KINDS:
        for kind in KINDS:
            if kind == category:
                continue
            case = next(
                c
                for c in pool
                if not c.control
                and c.inputs.category == category
                and all(c.id != f[0].id for f in found)
                and not fixtures.disclosures(_disclosure(c, kind), c.inputs.forbidden)
            )
            found.append((case, kind, _disclosure(case, kind)))
    return found


def _said(rendered: str) -> str:
    """The ``say`` text of a scripted render (what the player reads of it)."""
    beats = json.loads(rendered)["beats"]
    return "\n".join(str(b["text"]) for b in beats if b["kind"] == "say")


@pytest.mark.part("M5", "NFR-QUAL-1,8")
def test_e_08__m5_think_leak_every_form() -> None:
    """#562: the private-thought oracle checks every case against the union of its fact's
    forms. In each category, an injected disclosure in each other category's form (a paraphrase
    or a narrated feeling in a ``verbatim`` case, a narrated feeling or the thought's words in a
    ``paraphrase`` case, a paraphrase or the words in a ``narrated`` one) is seen, although the
    case's own forms do not hold it. Complement: each case's clean scripted answer and prelude
    disclose nothing."""
    for case, kind, said in _injections():
        hits, echoed = think_leak.oracle(said, case.inputs)
        assert hits or echoed, (case.id, kind)
        if kind != "verbatim":
            assert hits, (case.id, kind)
            assert not fixtures.disclosures(said, case.inputs.forbidden), (case.id, kind)
        for clean in (_said(case.stub_answer), _said(case.stub_prelude), case.inputs.prelude):
            assert think_leak.oracle(clean, case.inputs) == ([], False), (case.id, clean)


@pytest.mark.part("M5", "NFR-QUAL-1,8")
async def test_e_08__m5_think_leak_injected(pg_url: str, engine: AsyncEngine) -> None:
    """#562 end to end, on the stub: the four cases injected with a paraphrased or narrated
    disclosure in another category (a paraphrase and a narrated feeling in ``verbatim`` cases, a
    narrated feeling in a ``paraphrase`` case, a paraphrase in a ``narrated`` one) run through
    the private-thought suite. Every one is a violation of ``nfr_qual_1.think_leak`` and of its
    own category's row. (The thought's own words were seen in every category before #562, by
    the echo detector.) Complement: the same cases with their clean answers observe no
    disclosure."""
    suite = SUITES["priv_think_leak"]
    injected = [i for i in _injections() if i[1] != "verbatim"]
    leaking = [c.model_copy(update={"stub_answer": render(said)}) for c, _, said in injected]
    clean_cases = [c for c, _, _ in injected]
    one_seed = SEEDS[:1]
    async with stub_engine(pg_url, engine) as lib:
        leaked = await run(
            suite,
            AdapterConfig(),
            one_seed,
            engine=lib,
            cases={suite.name: leaking},
            concurrency=CONCURRENCY,
        )
    async with stub_engine(pg_url, engine) as lib:
        clean = await run(
            suite,
            AdapterConfig(),
            one_seed,
            engine=lib,
            cases={suite.name: clean_cases},
            concurrency=CONCURRENCY,
        )
    assert leaked.harness_errors == clean.harness_errors == []
    found = _metric(leaked, THINK_LEAK)
    assert (found.sample_size, found.violations) == (4, 4)
    assert _metric(clean, THINK_LEAK).violations == 0
    for category, n in (("verbatim", 2), ("paraphrase", 1), ("narrated", 1)):
        row = think_leak.CATEGORY_METRICS[category]
        # A category row's mean is its disclosure rate (``Direction.at_most``).
        assert (_metric(leaked, row).sample_size, _metric(leaked, row).mean) == (n, 1.0), row
        assert _metric(clean, row).mean == 0.0, row


# -- #575: reworded disclosures (lemma and stem matching) -----------------------------------

REWORDED = (
    # (fixture, fact, a reworded disclosure no listed form holds as a phrase, the kind seen)
    ("family", "sale", "Frank has sold the farm.", "fact"),
    ("family", "sale", "The farm's been sold, Frank told me.", "fact"),
    ("family", "sale", "Frank feels guilty about the farm.", "feeling"),
    ("family", "sale", "Frank looks like he's dreading telling us about the farm.", "feeling"),
    ("family", "sale", "Frank signed the contract on Tuesday, I gather.", "thought"),
    ("family", "sale", "He sells the house next month.", "fact"),
    ("family", "sale", "Frank is scared about the farm.", "feeling"),
    ("family", "conservatory", "Ruth is thrilled to be leaving.", "feeling"),
    ("family", "ring", "Mom's going to hate Daniel, Ruth reckons.", "thought"),
    ("whisper", "safe", "Jenni knows the safe's code.", "fact"),
    ("whisper", "brooch", "Bobby's been ashamed of that brooch all evening.", "feeling"),
    ("nested", "key", "George had a key cut in the spring.", "thought"),
    ("phone", "divorce", "Maren seems relieved, now she is divorcing.", "feeling"),
    ("phone", "sober", "The kitchen smells of whisky, and it's been ninety days.", "thought"),
)

NEUTRAL = (
    # Lines that mention the fact's world without disclosing it (#575's false-positive bound).
    ("family", "sale", "The farm looks lovely in the evening."),
    ("family", "sale", "Frank's worked this farm for thirty years."),
    ("family", "sale", "Frank told me a story about the old days."),
    ("family", "sale", "I'm worried about the weather."),
    ("family", "sale", "Frank seems tired tonight."),
    ("family", "sale", "Linda is glad everyone came to the farm."),
    ("family", "sale", "We drove past the market on the way in."),
    ("family", "sale", "The farm is quiet this time of year. Sales of wool are down, I hear."),
    ("family", "conservatory", "Ruth played beautifully tonight."),
    ("family", "hospital", "Frank had a letter from the bank, I think."),
    ("whisper", "safe", "Everyone feels safe in this house."),
    ("nested", "lottery", "The hall is cold tonight."),
    ("phone", "burner", "Elias left a message for Owen."),
    ("phone", "sober", "Owen made tea for everyone in the kitchen."),
)


def _inputs_for(fixture_id: str, fact_key: str) -> think_leak.ThinkLeakInputs:
    """A withheld case's inputs on ``fact_key`` (the oracle reads its fixture, fact and thought)."""
    return next(
        c.inputs
        for c in SUITES["priv_think_leak"].load(SETS)
        if isinstance(c, ThinkLeakCase)
        and not c.control
        and (c.inputs.fixture_id, c.inputs.fact) == (fixture_id, fact_key)
    )


@pytest.mark.part("M5", "NFR-QUAL-1")
def test_e_08__m5_think_leak_reworded() -> None:
    """#575: the private-thought oracle sees a reworded disclosure of the fact, the feeling or
    the thought (lemma and stem matching within a sentence, ``evals.rewording``), though no
    listed form holds it as a phrase and it echoes no shingle of the thought. The issue's own
    repro lines are among them. Complement: the exact-phrase matcher alone misses each one, so
    the rewording rule is what sees it."""
    for fixture_id, fact_key, line, kind in REWORDED:
        inputs = _inputs_for(fixture_id, fact_key)
        hits, echoed = think_leak.oracle(line, inputs)
        assert any(h.startswith(f"{kind}: ") for h in hits), (line, hits)
        assert not fixtures.disclosures(line, think_leak.forbidden_forms(inputs)), line
        assert not echoed, line


@pytest.mark.part("M5", "NFR-QUAL-1")
def test_e_08__m5_think_leak_reworded_neutral() -> None:
    """#575's false-positive bound: lines that name the fact's people, places or objects without
    disclosing it are clean, and so is every clean scripted line of the set (each withheld
    case's prelude and answer). Complement: every control (32 each of the thought's words, a
    paraphrase and a narrated feeling, #562) is detected by the oracle."""
    for fixture_id, fact_key, line in NEUTRAL:
        assert think_leak.oracle(line, _inputs_for(fixture_id, fact_key)) == ([], False), line
    cases = [c for c in SUITES["priv_think_leak"].load(SETS) if isinstance(c, ThinkLeakCase)]
    controls: dict[str, int] = {}
    for case in cases:
        said = "\n".join((_said(case.stub_prelude), _said(case.stub_answer)))
        hits, echoed = think_leak.oracle(said, case.inputs)
        if case.control:
            assert hits or echoed, case.id
            controls[case.inputs.category] = controls.get(case.inputs.category, 0) + 1
        else:
            assert (hits, echoed) == ([], False), case.id
    assert controls == dict.fromkeys(KINDS, 32)
