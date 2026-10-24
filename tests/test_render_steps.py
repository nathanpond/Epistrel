"""A render's declared steps (FR-TURN-12, FR-EPI-14; #168): the schema's structural rules
(``render.StepDecl``, invalid structure the model retries) and the ordered verification
(``orchestrator.structured.steps``), without a database."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from epistrel.core.antecedents import RecordAntecedent
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.orchestrator.structured import steps as s
from epistrel.orchestrator.structured.render import render_schema

SKETCH = {"subject": "the bridge", "relation": "condition", "object": "collapsed"}


def _render(beats: list[dict[str, Any]], steps: list[dict[str, Any]], **kwargs: Any) -> Any:
    schema = render_schema(
        actor_id=kwargs.pop("actor", "jenni"), player_id="cal", cast_ids=["jenni"], max_beats=6
    )
    return schema.model_validate({"beats": beats, "steps": steps})


DO = {"kind": "do", "text": "She heads for the ferry.", "step": 3}
GOOD = [
    {"index": 1, "kind": "observe", "proposition": SKETCH},
    {"index": 2, "kind": "believe", "cites": [1], "proposition": SKETCH},
    {"index": 3, "kind": "act", "cites": [2]},
]


def test_render_steps__structure() -> None:
    """The structural rules; an order violation (a later cite) is not structural."""
    found = _render([DO], GOOD)
    assert [x.kind for x in found.steps] == ["observe", "believe", "act"]
    later = [
        {"index": 1, "kind": "believe", "cites": [2], "proposition": SKETCH},
        {"index": 2, "kind": "observe", "proposition": SKETCH},
        {"index": 3, "kind": "act", "cites": [1]},
    ]
    assert _render([DO], later).steps[0].step_cites() == [2]
    mixed = _render(
        [DO, {"kind": "think", "text": "It fell.", "cites": ["e1"]}],
        [GOOD[0], {**GOOD[1], "cites": [1, "e1"]}, GOOD[2]],
    )
    assert mixed.steps[1].record_cites() == ["e1"]
    bad: list[tuple[list[dict[str, Any]], list[dict[str, Any]], str]] = [
        ([DO], [GOOD[0], {**GOOD[1], "index": 3}, {**GOOD[2], "index": 2}], "1..n"),
        ([DO], [GOOD[0], GOOD[0], GOOD[2]], "1..n"),
        ([DO], [GOOD[0], {**GOOD[1], "cites": [2]}, GOOD[2]], "itself"),
        ([DO], [GOOD[0], {**GOOD[1], "cites": [7]}, GOOD[2]], "no step"),
        ([DO], [GOOD[0], {**GOOD[1], "cites": [3]}, GOOD[2]], "never cites an act"),
        ([DO], [GOOD[0], {"index": 2, "kind": "believe", "cites": [1]}, GOOD[2]], "proposition"),
        ([DO], [{**GOOD[0], "cites": [2]}, GOOD[1], GOOD[2]], "cites nothing"),
        (
            [DO],
            [
                {"index": 1, "kind": "believe", "cites": [2], "proposition": SKETCH},
                {"index": 2, "kind": "believe", "cites": [1], "proposition": SKETCH},
                {"index": 3, "kind": "act"},
            ],
            "cycle",
        ),
        ([{**DO, "step": 1}], GOOD, "names no act step"),
        ([DO], [], "declares no steps"),
        ([DO, {"kind": "think", "text": "It fell.", "cites": ["e9"]}], GOOD, "disagrees"),
        ([{"kind": "think", "text": "Hm.", "step": 3}, DO], GOOD, "step"),
    ]
    for beats, steps, message in bad:
        with pytest.raises(ValidationError, match=message):
            _render(beats, steps)
    with pytest.raises(ValidationError, match="only a character"):
        _render([{"kind": "narration", "text": "Rain."}], [GOOD[2]], actor=None)


IDS = {name: new_ulid() for name in ("bridge", "condition")}


def _prop(obj: str) -> Proposition:
    ids = IDS.setdefault(obj, new_ulid())
    return Proposition(
        subject_id=IDS["bridge"],
        predicate_id=IDS["condition"],
        object_id=ids,
        valid_time=Interval.of(None, None),
    )


def _step(index: int, kind: str, *cites: Any, obj: str | None = "collapsed") -> s.Step:
    return s.Step(
        index,
        kind,
        step_cites=tuple(c for c in cites if isinstance(c, int)),
        record_cites=tuple(c for c in cites if isinstance(c, str)),
        proposition=None if kind == s.ACT or obj is None else _prop(obj),
    )


async def _seen(step: s.Step) -> s.Observed:
    assert step.proposition is not None
    if step.proposition.object_id == IDS["collapsed"]:
        return s.Observed(True, event_id="e-fact")
    return s.Observed(False, s.UNVERIFIED_OBJECT)


async def _probe(record_id: str) -> RecordAntecedent | None:
    if record_id == "u1":
        return RecordAntecedent("utterances", "u1", None, True, speaker_id="bobby")
    return None


async def test_render_steps__order() -> None:
    """Steps verify in index order: a believe needs a verified earlier step or a record; a
    later cite, an unverified cite or no cite is self-attestation, and its dependents fall."""
    staged: list[int] = []
    found = s.StepVerifier(
        [
            _step(1, s.OBSERVE),
            _step(2, s.BELIEVE, 1, obj="impassable"),
            _step(3, s.OBSERVE, obj="intact"),
            _step(4, s.BELIEVE, 3, obj="intact"),
            _step(5, s.BELIEVE, 6, obj="x"),
            _step(6, s.BELIEVE, 2, obj="y"),
            _step(7, s.BELIEVE, obj="z"),
            _step(8, s.BELIEVE, "u1", obj="w"),
            _step(9, s.BELIEVE, "nope", obj="v"),
            _step(10, s.ACT, 2),
        ],
        _seen,
        _probe,
        on_verified=lambda step, _: staged.append(step.index),
    )
    await found.run_before(3)
    assert sorted(found.verdicts) == [1, 2]
    verdicts = await found.run_all()
    assert staged == [1, 2, 6, 8, 10]
    assert (verdicts[2].cited_step, verdicts[2].transition_kind) == (1, "observation")
    assert (verdicts[6].cited_step, verdicts[6].transition_kind) == (2, "inference")
    assert verdicts[8].transition_kind == "testimony"
    record = verdicts[8].record
    assert record is not None
    assert record.speaker_id == "bobby"
    assert {i: (v.reason, v.cause) for i, v in verdicts.items() if not v.verified} == {
        3: (s.UNVERIFIED_OBJECT, None),
        4: (s.SELF_ATTESTATION, s.UNVERIFIED_CITE),
        5: (s.SELF_ATTESTATION, s.LATER_CITE),
        7: (s.SELF_ATTESTATION, s.NO_CITE),
        9: (s.ANTECEDENT_INVALID, None),
    }
    report = found.report()
    assert (report["declared"], report["verified"]) == (10, 5)
    assert s.first_citing_act(found.steps, 2) == 10


def test_render_steps__oracle() -> None:
    """The oracle answers a verified step of this operation and variant at its position; the
    positions order by step within the render's first beat."""
    positions = {i: s.position_of(4, 0, 2, i) for i in (1, 2, 3)}
    oracle = s.StepOracle("op", 0, positions, frozenset({1, 3}))
    found = oracle.render_step({"operation_id": "op", "variant": 0, "beat": 2, "step": 1})
    assert found is not None
    assert found.position == Position(4, 0, 0, 2, 1)
    assert oracle.render_step({"operation_id": "op", "variant": 0, "beat": 2, "step": 2}) is None
    assert oracle.render_step({"operation_id": "x", "variant": 0, "beat": 2, "step": 1}) is None
    assert oracle.render_step({"operation_id": "op", "variant": 1, "beat": 2, "step": 1}) is None
    assert oracle.render_step({"operation_id": "op", "variant": 0, "beat": 3, "step": 1}) is None
    assert oracle.staged_beat({"operation_id": "op"}) is None
    assert positions[1].before(positions[2])
    assert positions[3].before(Position(4, 0, 0, 3, 0))
