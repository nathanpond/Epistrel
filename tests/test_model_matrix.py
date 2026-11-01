"""The model matrix tool's library (NFR-PORT-3, NFR-PERF-8; #54): percentiles, rows, merge and
rendering, the call breakdown (#303), P-08's calls assertion against recorded turns (#308),
and E-06's prompt-parameter check against the stub. The end-to-end runs against a local
OpenAI-compatible server and against the stub with scripted deadline faults are
``tests/integration/test_model_matrix_run.py``."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from epistrel.core.profiles.schema import MAX_DEADLINE_MS
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.capability import check_prompt_params, context_prompt
from epistrel.orchestrator.models.metrics import CallLog, ModelCall, Outcome
from epistrel.orchestrator.policies.output_gate import DEADLINE
from epistrel.tools import model_matrix as mm
from tests.auth_support import auth_settings
from tests.perf.test_p_08 import check_rows

MINORS_DESIGN = {"generation": 1, "guardrail_check": 2}
"""A minors-row turn's plan: one generation, the input check and the output check."""


def row(**changes: Any) -> mm.Row:
    values: dict[str, Any] = {
        "tier": "local",
        "model": "m",
        "policies": "off",
        "measured_at": "2026-10-11",
        "sha": "abc1234",
        "hardware": "laptop",
        "server_version": "server 1",
        "turn_path": mm.TURN_PATH,
        "protocol": True,
        "protocol_reason": None,
        "capability": {
            "generation": {"capable": True, "checks": [{"name": "context", "passed": True}]},
            "guardrail_check": {
                "capable": False,
                "checks": [
                    {"name": "context", "passed": True},
                    {"name": "structured_output", "passed": False},
                ],
            },
        },
        "e06": {"passed": True, "lengths": {"32": 30}, "identical": 10, "samples": 10},
        "turns": 3,
        "p50_ms": 1200,
        "p95_ms": 2500,
        "calls_per_turn": {"generation": 1.0},
        "latencies_ms": [1000, 1200, 2500],
    }
    values.update(changes)
    return mm.Row(**values)


def entry(purpose: str, outcome: Outcome = "ok", attempt: int = 1) -> ModelCall:
    return ModelCall(
        purpose=purpose,
        model="m",
        attempt=attempt,
        outcome=outcome,
        latency_ms=1,
        tokens_in=1,
        tokens_out=1,
    )


def on_design() -> list[ModelCall]:
    """A minors-row turn that made exactly its designed calls: input check, generation, output
    check."""
    return [entry("guardrail_check"), entry("generation"), entry("guardrail_check")]


def scripted_turns() -> list[mm.TurnCalls]:
    """Five minors-row turns, as #303's live run had them and worse:

    1. on design;
    2. the output check flagged the draft, and the regeneration was cut at the deadline;
    3. the regeneration answered, and its re-check was cut at the deadline;
    4. the input check answered garbage and its structured retry answered;
    5. the deadline passed before the output check, so no call was made to fail."""
    deadline = [DEADLINE]
    return [
        mm.TurnCalls(on_design(), MINORS_DESIGN),
        mm.TurnCalls([*on_design(), entry("generation", "timeout")], MINORS_DESIGN, deadline),
        mm.TurnCalls(
            [*on_design(), entry("generation"), entry("guardrail_check", "timeout")],
            MINORS_DESIGN,
            deadline,
        ),
        mm.TurnCalls(
            [
                entry("guardrail_check", "invalid_output"),
                entry("guardrail_check", attempt=2),
                entry("generation"),
                entry("guardrail_check"),
            ],
            MINORS_DESIGN,
        ),
        mm.TurnCalls([entry("guardrail_check"), entry("generation")], MINORS_DESIGN, deadline),
    ]


def test_nearest_rank() -> None:
    values = [15, 20, 35, 40, 50]
    assert mm.nearest_rank(values, 50) == 35
    assert mm.nearest_rank(values, 95) == 50
    assert mm.nearest_rank(values, 100) == 50
    assert mm.nearest_rank(list(range(1, 21)), 95) == 19
    assert mm.nearest_rank([7], 50) == 7
    with pytest.raises(ValueError, match="no values"):
        mm.nearest_rank([], 50)
    with pytest.raises(ValueError, match="percentile"):
        mm.nearest_rank(values, 0)


def test_calls_per_turn_by_purpose() -> None:
    one, three = CallLog(), CallLog()
    one.calls.append(entry("generation"))
    three.calls += [entry("guardrail_check"), entry("generation"), entry("guardrail_check")]
    assert mm.calls_per_turn([one, three]) == {"generation": 1.0, "guardrail_check": 1.0}
    assert mm.calls_per_turn([three]) == {"generation": 1.0, "guardrail_check": 2.0}


def test_designed_calls() -> None:
    """A Lite turn is designed to make one ``generation`` call and the guardrail calls G its
    budget reserves (#69). The guarded reply's G is one check per model-assisted policy for
    each of the input and the reply it applies to: the minors row's in-memory policy applies to
    both, so 2, and policies off make none."""
    assert mm.designed_calls(0) == {"generation": 1}
    assert mm.designed_calls(2) == {"generation": 1, "guardrail_check": 2}
    assert mm.guarded_checks(mm.policy_set("minors")) == 2
    assert mm.guarded_checks(mm.policy_set("off")) == 0


def test_call_breakdown_separates_the_design_from_extra_calls() -> None:
    """#303: the mean calls per turn (``3.6`` here) folds extra calls and failed attempts into
    one number; the breakdown separates them. Per purpose it reports the designed calls per
    turn and the calls beyond them; it reports the failed calls by outcome; it lists the turns
    that hit a model-call deadline or error, which includes a turn whose deadline released the
    safe fallback before any call could fail; and it counts the safe fallbacks by cause."""
    turns = scripted_turns()
    assert mm.calls_per_turn(turns) == {"generation": 1.4, "guardrail_check": 2.2}
    breakdown = mm.call_breakdown(turns)
    assert breakdown.designed_calls_per_turn == {"generation": 1.0, "guardrail_check": 2.0}
    assert breakdown.extra_calls == {"generation": 2, "guardrail_check": 2}
    assert breakdown.failed_calls == {
        "generation": {"timeout": 1},
        "guardrail_check": {"invalid_output": 1, "timeout": 1},
    }
    assert breakdown.faulted_turns == [2, 3, 4, 5]
    assert breakdown.fallbacks == {"deadline": 3}


def test_call_breakdown_of_turns_on_design() -> None:
    """Complement of the test above: turns that made exactly their designed calls report no
    extra or failed calls, no faulted turn and no fallback, and a whole-number calls per turn
    equal to the design."""
    turns = [mm.TurnCalls(on_design(), MINORS_DESIGN) for _ in range(3)]
    breakdown = mm.call_breakdown(turns)
    assert breakdown.extra_calls == {}
    assert breakdown.failed_calls == {}
    assert breakdown.faulted_turns == []
    assert breakdown.fallbacks == {}
    per_turn = mm.calls_per_turn(turns)
    assert per_turn == breakdown.designed_calls_per_turn == {"generation": 1, "guardrail_check": 2}
    assert all(float(count).is_integer() for count in per_turn.values())


CAP = 7
"""A recorded turn's call cap (``budget.max_calls``) in the P-08 tests below."""
OFF_DESIGN = {"generation": 1}


def minors(calls: list[ModelCall], *causes: str, cap: int | None = CAP) -> mm.TurnCalls:
    return mm.TurnCalls(calls, MINORS_DESIGN, causes, cap)


def off_turns(n: int = 3) -> list[mm.TurnCalls]:
    return [mm.TurnCalls([entry("generation")], OFF_DESIGN, (), CAP) for _ in range(n)]


def flagged_regenerated() -> list[ModelCall]:
    """The output check flagged a benign draft, the output gate regenerated and the re-check
    passed (#68): two calls beyond the design, no fault."""
    return [*on_design(), entry("generation"), entry("guardrail_check")]


def recorded_minors() -> list[mm.TurnCalls]:
    """A minors row as a live run records it: #303's five turns (two cut by the deadline in a
    regeneration or a re-check, a structured retry, a deadline before the output check), each
    with its cap, plus a flagged draft regenerated and re-checked."""
    return [minors(turn.calls, *turn.fallback_causes) for turn in scripted_turns()] + [
        minors(flagged_regenerated())
    ]


def old_p_08(rows: dict[str, list[mm.TurnCalls]]) -> None:
    """P-08's calls assertion before #308: the folded mean."""
    off, on = mm.calls_per_turn(rows["off"]), mm.calls_per_turn(rows["minors"])
    assert off == {"generation": 1.0}
    assert on["guardrail_check"] == 2.0
    assert sum(on.values()) > sum(off.values())


def test_p_08_asserts_the_design_of_flagged_and_deadline_turns() -> None:
    """#308: on recorded turns with a flagged draft regenerated and re-checked and turns the
    deadline cut, the folded mean is ``guardrail_check 2.33``, so the old assertion fails
    though the design held. ``check_rows`` passes: the designed calls per turn are
    ``generation 1, guardrail_check 2`` and ``generation 1``, and the extra calls are
    reported, each turn within its cap."""
    rows = {"off": off_turns(), "minors": recorded_minors()}
    assert mm.calls_per_turn(rows["minors"]) == {"generation": 1.5, "guardrail_check": 2.33}
    with pytest.raises(AssertionError):
        old_p_08(rows)
    breakdowns = check_rows(rows)
    assert breakdowns["minors"].designed_calls_per_turn == {"generation": 1, "guardrail_check": 2}
    assert breakdowns["minors"].extra_calls == {"generation": 3, "guardrail_check": 3}
    assert breakdowns["minors"].faulted_turns == [2, 3, 4, 5]
    assert breakdowns["minors"].fallbacks == {"deadline": 3}
    assert breakdowns["off"].designed_calls_per_turn == {"generation": 1}
    assert breakdowns["off"].extra_calls == {}


def test_p_08_passes_turns_on_design_as_before() -> None:
    """Complement: turns on design pass both the old assertion and ``check_rows``."""
    rows = {"off": off_turns(), "minors": [minors(on_design()) for _ in range(3)]}
    old_p_08(rows)
    assert check_rows(rows)["minors"].extra_calls == {}


WRONG_MINORS: dict[str, tuple[list[mm.TurnCalls], str]] = {
    "output check not designed": (
        [
            mm.TurnCalls(
                [entry("guardrail_check"), entry("generation")],
                OFF_DESIGN | {"guardrail_check": 1},
                (),
                CAP,
            )
        ],
        "turn 1: designed",
    ),
    "a check skipped with no fault": (
        [minors(on_design()), minors([entry("guardrail_check"), entry("generation")])],
        "turn 2: 1 of 2 designed guardrail_check calls, with no deadline or error",
    ),
    "a regeneration released unchecked": (
        [minors([*on_design(), entry("generation")])],
        "turn 1: its last draft was released unchecked",
    ),
    "a call outside the design": (
        [minors([*on_design(), entry("extraction")])],
        "turn 1: 1 extraction calls outside the design",
    ),
    "a turn over its cap": (
        [minors(flagged_regenerated() * 2)],
        f"turn 1: 10 calls, over its budget of {CAP}",
    ),
}


@pytest.mark.parametrize("case", sorted(WRONG_MINORS))
def test_p_08_fails_a_wrong_minors_row(case: str) -> None:
    """#308: a genuinely wrong run still fails ``check_rows``, and ``design_problems`` names
    the turn and the fault."""
    turns, problem = WRONG_MINORS[case]
    assert any(p.startswith(problem) for p in mm.design_problems(turns, MINORS_DESIGN))
    with pytest.raises(AssertionError):
        check_rows({"off": off_turns(), "minors": turns})


def test_p_08_fails_a_wrong_run_the_old_assertion_passed() -> None:
    """A skipped check in one turn and a regeneration in another fold into a mean of exactly
    ``guardrail_check 2.0``: the old assertion passed it, ``check_rows`` fails it."""
    skipped = minors([entry("guardrail_check"), entry("generation")])
    rows = {"off": off_turns(), "minors": [skipped, minors(flagged_regenerated())]}
    old_p_08(rows)
    with pytest.raises(AssertionError, match="turn 1: 1 of 2 designed guardrail_check"):
        check_rows(rows)


def test_p_08_fails_a_wrong_off_row_and_an_unbounded_turn() -> None:
    """A policies-off turn that makes a ``guardrail_check`` call fails, and so does a turn
    that recorded no call cap, since its extra calls would be unbounded."""
    checked = [mm.TurnCalls([entry("generation"), entry("guardrail_check")], OFF_DESIGN, (), CAP)]
    assert mm.design_problems(checked, OFF_DESIGN) == [
        "turn 1: 1 guardrail_check calls outside the design"
    ]
    with pytest.raises(AssertionError):
        check_rows({"off": checked, "minors": recorded_minors()})
    uncapped = [*recorded_minors(), minors(on_design(), cap=None)]
    assert mm.design_problems(uncapped, MINORS_DESIGN) == []
    with pytest.raises(AssertionError):
        check_rows({"off": off_turns(), "minors": uncapped})


def test_merge_replaces_same_key_and_keeps_others() -> None:
    other_tier = asdict(row(tier="hosted", model="big"))
    stale = asdict(row(p50_ms=9999))
    merged = mm.merge([other_tier, stale], [row(), row(policies="minors")])
    assert [r["policies"] for r in merged if r["tier"] == "local"] == ["minors", "off"]
    assert [r["p50_ms"] for r in merged if r["tier"] == "local"] == [1200, 1200]
    assert any(r["tier"] == "hosted" for r in merged)
    assert len(merged) == 3


def test_render_two_rows(tmp_path: Path) -> None:
    rows = mm.merge(
        [],
        [row(), row(policies="minors", calls_per_turn={"generation": 1.0, "guardrail_check": 2.0})],
    )
    markdown, raw = tmp_path / "m.md", tmp_path / "m.json"
    mm.write_matrix(rows, markdown, raw)
    text = markdown.read_text("utf-8")
    assert "**Reported, not gated.**" in text
    assert "| local: m | policies off | ✓ |" in text
    assert "| local: m | minors policy on | ✓ |" in text
    assert "✗ (context ✓; structured_output ✗)" in text
    assert "3 (generation 1, guardrail_check 2)" in text
    assert "1.20 s | 2.50 s" in text
    assert "`abc1234`" in text
    assert json.loads(raw.read_text("utf-8"))["rows"][0]["latencies_ms"] == [1000, 1200, 2500]
    assert mm.load_rows(raw) == rows
    assert mm.load_rows(tmp_path / "absent.json") == []


def test_e06_lengths_in_cap_order() -> None:
    """The E-06 cell lists its lengths by cap, whatever the row's key order. The json stores
    them sorted as strings ("128" before "32"), so re-rendering from the committed file
    reordered the cell. This was found while re-rendering the matrix for #303.

    Complement asserted: the order the tool holds in memory renders the same."""
    for lengths in ({"128": 120, "32": 30}, {"32": 30, "128": 120}):
        e06 = {"passed": True, "lengths": lengths, "identical": 10, "samples": 10}
        text = mm.render(mm.merge([], [row(e06=e06)]))
        assert "(tokens 30/32, 120/128; temp 0: 10/10 identical)" in text


def test_render_empty() -> None:
    text = mm.render([])
    assert "No tier has been measured yet." in text
    assert "model-matrix.json" in text


def breakdown_rows() -> list[dict[str, Any]]:
    """#303's live run in miniature: the minors row's turns 2 and 4 hit the deadline (one
    regeneration cut short, one re-check cut short) and are its two slow turns; the policies-off
    row stayed on design."""
    minors = row(
        policies="minors",
        turns=5,
        latencies_ms=[1000, 2050, 1200, 2010, 1100],
        p50_ms=1200,
        p95_ms=2050,
        calls_per_turn={"generation": 1.4, "guardrail_check": 2.2},
        designed_calls_per_turn={"generation": 1.0, "guardrail_check": 2.0},
        extra_calls={"generation": 2, "guardrail_check": 1},
        failed_calls={"generation": {"timeout": 1}, "guardrail_check": {"timeout": 1}},
        faulted_turns=[2, 4],
        fallbacks={"deadline": 2},
    )
    off = row(
        designed_calls_per_turn={"generation": 1.0},
        extra_calls={},
        failed_calls={},
        faulted_turns=[],
        fallbacks={},
    )
    return mm.merge([], [off, minors])


def test_render_the_call_breakdown(tmp_path: Path) -> None:
    """#303: the md separates the designed calls from the extra ones per purpose, gives the
    failed calls by outcome, the turns that hit a deadline or error, the p50/p95 of the other
    turns (so the 2 s outliers can be traced to the deadline from the md alone) and the safe
    fallbacks. The header explains each of these columns, and the json carries the same
    breakdown.

    Complement asserted: the row that stayed on design reports zeros and its p50/p95 unchanged,
    and with every row recorded there is no "not recorded" note."""
    rows = breakdown_rows()
    markdown, raw = tmp_path / "m.md", tmp_path / "m.json"
    mm.write_matrix(rows, markdown, raw)
    text = markdown.read_text("utf-8")
    assert (
        "| local: m | minors policy on | 3 (generation 1, guardrail_check 2) "
        "| 3 (generation 2, guardrail_check 1) "
        "| 2 (generation timeout 1, guardrail_check timeout 1) | 2 of 5 (turns 2, 4) "
        "| 1.10 s / 1.20 s | 2 (deadline 2) |"
    ) in text
    on_design_row = "| 1 (generation 1) | 0 | 0 | 0 of 3 | 1.20 s / 2.50 s | 0 |"
    assert f"| local: m | policies off {on_design_row}" in text
    for column in (
        "Designed per turn",
        "Extra calls",
        "Failed calls",
        "Turns with a deadline or error",
        "p50 / p95 without them",
        "Safe fallbacks",
    ):
        assert f"**{column}**" in text
    assert "not recorded" not in text.lower()
    minors = next(
        r for r in json.loads(raw.read_text("utf-8"))["rows"] if r["policies"] == "minors"
    )
    assert minors["designed_calls_per_turn"] == {"generation": 1.0, "guardrail_check": 2.0}
    assert minors["extra_calls"] == {"generation": 2, "guardrail_check": 1}
    assert minors["failed_calls"] == {
        "generation": {"timeout": 1},
        "guardrail_check": {"timeout": 1},
    }
    assert minors["faulted_turns"] == [2, 4]
    assert minors["fallbacks"] == {"deadline": 2}


def test_rows_without_the_breakdown_are_not_recorded(tmp_path: Path) -> None:
    """A row measured before #303 kept only the mean calls per turn: loading it marks every
    breakdown field ``None`` (written back as ``null``), and the md shows *not recorded* in
    each breakdown cell under a header note that the next live run fills them. Nothing is
    derived from the mean.

    Complement asserted: the note goes once every row has the breakdown."""
    legacy = {k: v for k, v in asdict(row(policies="minors")).items() if k not in mm.BREAKDOWN}
    raw = tmp_path / "m.json"
    raw.write_text(json.dumps({"rows": [legacy]}), "utf-8")
    loaded = mm.load_rows(raw)
    assert all(loaded[0][name] is None for name in mm.BREAKDOWN)
    assert {k: v for k, v in loaded[0].items() if k not in mm.BREAKDOWN} == legacy
    markdown = tmp_path / "m.md"
    mm.write_matrix(loaded, markdown, raw)
    assert all(r[name] is None for r in mm.load_rows(raw) for name in mm.BREAKDOWN)
    assert '"faulted_turns": null' in raw.read_text("utf-8")
    text = markdown.read_text("utf-8")
    assert "**Not recorded.**" in text
    assert "next live run" in text
    not_recorded = " | ".join(["not recorded"] * 6)
    assert f"| local: m | minors policy on | {not_recorded} |" in text
    assert "**Not recorded.**" not in mm.render(breakdown_rows())


def test_turn_path_note_only_while_a_guarded_reply_row_exists() -> None:
    """#303 (owner's comment): the **Turn path** note says a re-run through ``respond`` is
    pending, so it appears only while some row was measured on the guarded reply (#53).

    Complement asserted: a guarded-reply row brings it back."""
    respond_only = mm.render(mm.merge([], [row(), row(policies="minors")]))
    assert "**Turn path.**" not in respond_only
    assert "pending" not in respond_only
    mixed = mm.render(mm.merge([], [row(), row(tier="hosted", turn_path=mm.GUARDED_REPLY_PATH)]))
    assert "**Turn path.**" in mixed


def test_a_deadline_override_is_reported() -> None:
    """#454: a row measured under a turn deadline override says so in its **Runs** turn path,
    and the override note explains it, so a reader knows the profile default was not used.

    Complement asserted: rows without an override show neither."""
    plain = mm.render(mm.merge([], [row()]))
    assert "(override)" not in plain
    assert "**Turn deadline override (#454).**" not in plain
    structured = row(
        turn_kind="structured",
        turn_path=mm.STRUCTURED_PATH,
        deadline_override_ms=mm.MEASUREMENT_DEADLINE_MS,
    )
    text = mm.render(mm.merge([], [row(), structured]))
    assert "**Turn deadline override (#454).**" in text
    assert f"| {mm.STRUCTURED_PATH}; turn deadline 300 s (override) |" in text
    assert f"| {mm.TURN_PATH} |" in text


def test_the_deadline_override_scales_with_the_primaries() -> None:
    """#588 (the owner's option (b)): a structured row's turn deadline override is
    ``TURN_DEADLINE_S`` per primary the turn renders, capped at the profile schema's largest
    ``deadline_ms``, so a three-character turn is measured rather than cut at 300 s; the
    three-character drivers carry the scaled override, and a row records and reports the
    override it used.

    Complement asserted: a single-character structured row keeps 300 s, a Lite row has no
    override, and a turn with no primary is refused."""
    assert mm.measurement_deadline_ms(1) == mm.MEASUREMENT_DEADLINE_MS == 300_000
    assert mm.measurement_deadline_ms(2) == 600_000
    assert mm.measurement_deadline_ms(3) == MAX_DEADLINE_MS == 600_000
    assert (
        mm.measurement_deadline_ms(mm.THREE_CHARACTER_PRIMARIES) == mm.THREE_CHARACTER_DEADLINE_MS
    )
    with pytest.raises(ValueError, match="at least one primary"):
        mm.measurement_deadline_ms(0)
    overrides = {
        kind: getattr(mm.driver_for(kind), "deadline_override_ms", None) for kind in mm.TURN_KINDS
    }
    assert overrides == {
        "lite": None,
        "structured": 300_000,
        "repair": 300_000,
        "three_character_sequential": 600_000,
        "three_character_director": 600_000,
        "three_character_default": 600_000,
    }
    three = row(
        turn_kind="three_character_sequential",
        turn_path=mm.THREE_CHARACTER_PATH,
        deadline_override_ms=mm.THREE_CHARACTER_DEADLINE_MS,
    )
    assert asdict(three)["deadline_override_ms"] == 600_000
    text = mm.render(mm.merge([], [three]))
    assert f"| {mm.THREE_CHARACTER_PATH}; turn deadline 600 s (override) |" in text
    assert "300 s per primary" in text


def test_merge_failures_replaces_and_clears() -> None:
    """#454: a failure mark replaces an earlier mark of its key, and a row measured since
    clears its key's mark. Other keys' marks are kept."""
    failure = mm.RowFailure(
        tier="local",
        model="m",
        policies="off",
        turn_kind="structured",
        measured_at="2026-10-20",
        sha="abc1234",
        turn_path=mm.STRUCTURED_PATH,
        turn=1,
        code="model_unavailable",
        purpose=None,
        reason=None,
    )
    other = {**asdict(failure), "tier": "hosted"}
    older = {**asdict(failure), "turn": 7}
    assert mm.merge_failures([older, other], [], failure) == [other, asdict(failure)]
    measured = row(turn_kind="structured")
    assert mm.merge_failures([older, other], [measured], None) == [other]


def test_committed_matrix_is_its_json_rendered() -> None:
    """``docs/model-matrix.md`` is generated, never hand-edited: it is ``render`` of the
    committed ``docs/model-matrix.json``. A header change therefore reaches the committed file,
    and so does a note that has stopped applying (#303)."""
    assert mm.MATRIX_PATH.read_text("utf-8") == mm.render(
        mm.load_rows(mm.RAW_PATH),
        mm.load_embedding_rows(mm.RAW_PATH),
        mm.load_failed_rows(mm.RAW_PATH),
    )


def test_parser_purpose_model() -> None:
    args = mm.parser().parse_args(
        ["--base-url", "http://h/v1", "--model", "m", "--purpose-model", "guardrail_check=small"]
    )
    assert dict(args.purpose_model) == {"guardrail_check": "small"}
    assert args.tier == "local"
    assert args.turns == mm.DEFAULT_TURNS
    for bad in ("guardrail_check", "nope=m", "generation="):
        with pytest.raises(SystemExit):
            mm.parser().parse_args(["--base-url", "u", "--model", "m", "--purpose-model", bad])


def test_turn_kinds_key_label_and_parse() -> None:
    """P-08 M4 (#153): a structured or repair-heavy row has its own key beside the Lite row of
    the same tier, model and policies, its label names its turn kind, and the matrix explains
    turn kinds once such a row exists; a live run measures Lite and structured turns by
    default.

    Complement asserted: a Lite row keeps its pre-M4 key and label (the committed matrix
    re-renders unchanged), a row without ``turn_kind`` is the Lite row of its key, and
    ``repair`` (stub only) or an unknown kind is refused on the command line."""
    lite, structured = row(), row(turn_kind="structured")
    assert lite.key == "local|m|off"
    assert structured.key == "local|m|off|structured"
    merged = mm.merge([asdict(lite)], [structured, row(turn_kind="repair")])
    assert [r["turn_kind"] for r in merged] == ["lite", "repair", "structured"]
    text = mm.render(merged)
    assert "| local: m | policies off | ✓ |" in text
    assert "| local: m (structured turns) | policies off | ✓ |" in text
    assert "| local: m (repair-heavy turns) | policies off |" in text
    assert "**Turn kinds (P-08 M4).**" in text
    assert "**Turn kinds" not in mm.render(mm.merge([], [lite]))
    legacy = {k: v for k, v in asdict(lite).items() if k != "turn_kind"}
    assert mm.merge([legacy], [lite]) == [asdict(lite)]
    args = mm.parser().parse_args(["--base-url", "u", "--model", "m"])
    assert args.turn_kinds == ["lite", "structured"]
    only = mm.parser().parse_args(["--base-url", "u", "--model", "m", "--turn-kinds", "structured"])
    assert only.turn_kinds == ["structured"]
    for bad in ("repair", "lite,nope", ""):
        with pytest.raises(SystemExit):
            mm.parser().parse_args(["--base-url", "u", "--model", "m", "--turn-kinds", bad])


def test_turns_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    assert mm.main(["--base-url", "u", "--model", "m", "--turns", "0"]) == 2
    assert "--turns" in capsys.readouterr().err


def test_cast_has_a_minor_only_with_the_policy() -> None:
    assert min(c.true_age for c in mm.cast("minors")) < 18
    assert min(c.true_age for c in mm.cast("off")) >= 18
    assert mm.policy_set("off") == []
    assert [p.name for p in mm.policy_set("minors")] == ["minors"]


def test_context_prompt_size() -> None:
    text = "".join(m.content for m in context_prompt(8000))
    assert 8000 * 4 <= len(text) < 8000 * 4 + 500


async def test_e06_check_against_the_stub(dummy_dsn: str) -> None:
    """The deterministic stub honours ``max_tokens`` within its short answers and is fully
    repeatable at temperature 0. Complement: a nondeterministic stub is not repeatable."""
    layer = ModelAccessLayer(auth_settings(dummy_dsn, model_adapter="stub"))
    layer.bind(None)
    result = await check_prompt_params(layer)
    assert result.passed
    assert result.identical == result.samples == 10
    layer.stub.nondeterministic = True
    noisy = await check_prompt_params(layer)
    assert not noisy.repeatable
    assert not noisy.passed
    layer.stub.reset()
    layer.stub.fault("generation", "http_500", n=2)
    failing = await check_prompt_params(layer, samples=2)
    assert failing.lengths == {32: None, 128: None}
    assert not failing.lengths_ok
