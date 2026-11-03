"""#239's support-check recommendations (pure): ``keep`` when the amendment's parts still hold
against the active story, ``undo`` when they contradict it; a port recommends ``port`` on an
exact match of the replaced text and ``skip`` otherwise; every question names its amendment."""

from __future__ import annotations

from epistrel.orchestrator.rewrite.support_check import Finding, _options, recommend


def test_support_check_recommendations() -> None:
    assert recommend(True) == "keep"
    assert recommend(False) == "undo"
    for verifies, best in ((True, "keep"), (False, "undo")):
        options, recommended, _ = _options(
            Finding("A", "unsupported", (("events", "E"),), verifies)
        )
        actions = {o["id"]: o["effect"]["action"] for o in options}
        assert sorted(actions.values()) == ["keep", "rewrite", "undo"]
        assert actions[recommended] == best
        assert {o["effect"]["amendment_id"] for o in options} == {"A"}
    exact, recommended, _ = _options(
        Finding(
            "A",
            "port",
            (("events", "old"),),
            part_id="P",
            candidates=(("events", "near", False), ("events", "same", True)),
        )
    )
    chosen = next(o for o in exact if o["id"] == recommended)["effect"]
    assert (chosen["action"], chosen["record"]["id"], chosen["part_id"]) == ("port", "same", "P")
    loose, recommended, _ = _options(
        Finding(
            "A", "port", (("events", "old"),), part_id="P", candidates=(("events", "x", False),)
        )
    )
    assert next(o for o in loose if o["id"] == recommended)["effect"]["action"] == "skip"
    assert (
        Finding("A", "conflict", (("facts", "F"),)).key
        != Finding("A", "unsupported", (("facts", "F"),)).key
    )
