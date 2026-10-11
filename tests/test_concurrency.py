"""The FR-CONC-4 decision, read-set hashing and story settings, without a database (#47)."""

from __future__ import annotations

from functools import partial

import pytest
from pydantic import ValidationError

from epistrel.core.concurrency import (
    READ_SET_MEMBERS,
    Abandon,
    Commit,
    Rebase,
    Regenerate,
    RevalidationState,
    attempt_record,
    decide,
    hash_member,
    register_read_set_member,
    touched_members,
)
from epistrel.core.settings_schema import StorySettings


def _state(
    *, base: int = 4, current: int = 4, control: bool = False, touched: tuple[str, ...] = ()
) -> RevalidationState:
    return RevalidationState(
        attempt=1,
        base_revision=base,
        current_revision=current,
        control_since_base=control,
        control_operation_id="C" * 26 if control else None,
        touched=touched,
    )


def test_decide_follows_fr_conc_4() -> None:
    assert decide(_state()) == Commit()
    assert decide(_state(current=6)) == Rebase()
    assert decide(_state(current=5, touched=("pins",))) == Regenerate(
        reason="read_set_touched", touched=("pins",)
    )
    # A control action wins over everything else, even with a touched read set.
    assert decide(_state(current=7, control=True, touched=("window",))) == Abandon("C" * 26)


def test_decide_runs_the_recheck_only_at_rebase() -> None:
    calls: list[str] = []

    def failing() -> bool:
        calls.append("checked")
        return False

    strict = partial(decide, recheck=failing)
    assert strict(_state()) == Commit()
    assert strict(_state(current=5, touched=("window",))).name == "regenerate"
    assert calls == []
    assert strict(_state(current=5)) == Regenerate(reason="rebase_failed")
    assert calls == ["checked"]
    assert partial(decide, recheck=lambda: True)(_state(current=5)) == Rebase()


def test_attempt_records() -> None:
    assert attempt_record(_state(), Commit()) == {
        "attempt": 1,
        "base_revision": 4,
        "current_revision": 4,
        "outcome": "commit",
    }
    regen = attempt_record(_state(current=5), Regenerate("read_set_touched", ("pins",)))
    assert (regen["reason"], regen["touched"]) == ("read_set_touched", ["pins"])
    assert attempt_record(_state(control=True), Abandon("X"))["control_operation_id"] == "X"


def test_member_hashes_are_canonical() -> None:
    assert hash_member({"b": 1, "a": [1, "é"]}) == hash_member({"a": [1, "é"], "b": 1})
    assert hash_member(None) != hash_member([])
    assert len(hash_member("x")) == 64
    assert touched_members({"pins": "1", "window": "2"}, {"pins": "1", "window": "3"}) == (
        "window",
    )
    assert touched_members({"pins": "1"}, {"pins": "1", "segments": "2"}) == ("segments",)
    assert touched_members({"pins": "1"}, {"pins": "1"}) == ()


def test_read_set_members_are_fixed() -> None:
    assert READ_SET_MEMBERS == (
        "window",
        "segments",
        "pins",
        "instructions",
        "definitions",
        "guidance_text",
        "generation_settings",
    )
    with pytest.raises(ValueError, match="not a Lite read-set member"):
        register_read_set_member("guardrails", lambda conn, story, spec: None)  # type: ignore[arg-type,return-value]


def test_story_settings() -> None:
    assert StorySettings.from_row(None).regenerate_retry_budget == 2
    assert StorySettings.from_row({"regenerate_retry_budget": 0}).regenerate_retry_budget == 0
    kept = StorySettings.from_row({"turn_concurrency": "queue"})
    assert kept.model_dump()["turn_concurrency"] == "queue"  # later stories' settings survive
    with pytest.raises(ValidationError):
        StorySettings.from_row({"regenerate_retry_budget": -1})
