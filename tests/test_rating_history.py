"""The history evaluator of a rating tightening (FR-RATE-7; #118), without a database:
``core.ratings.change.evaluate_history`` over hand-built records and policies."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.policies.model import (
    Effect,
    EntityListRule,
    ModelAssistedRule,
    Policy,
    PolicyKind,
    Rule,
    Scope,
    Target,
    WordlistRule,
)
from epistrel.core.ratings.change import (
    IMAGE_UNCHECKED,
    CacheKey,
    HistoryRecord,
    evaluate_history,
    position_key,
    story_image_records,
)
from epistrel.core.ratings.stamp import stamp_of

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def _policy(
    pid: str,
    rule: Rule,
    *,
    scope: Scope = Scope.RATING,
    scope_id: str | None = "r-teen",
    applies_to: tuple[Target, ...] = (Target.TEXT_INPUT, Target.TEXT_OUTPUT),
    version: int = 1,
) -> Policy:
    kind = (
        PolicyKind.MODEL_ASSISTED
        if isinstance(rule, ModelAssistedRule)
        else PolicyKind.DETERMINISTIC
    )
    return Policy(
        id=pid,
        scope=scope,
        scope_id=scope_id,
        story_id="s-1" if scope in (Scope.STORY, Scope.CHARACTER) else None,
        name=f"name of {pid}",
        applies_to=applies_to,
        kind=kind,
        effect=Effect.DENY,
        rule=rule,
        age_condition=None,
        message="No.",
        version=version,
        created_by="p",
        updated_by="p",
        created_at=NOW,
        updated_at=NOW,
        retired_at=None,
    )


def _words(*words: str) -> WordlistRule:
    return WordlistRule(type="wordlist", words=words)


def _assisted(on_uncertain: str = "violation") -> ModelAssistedRule:
    return ModelAssistedRule.model_validate(
        {"type": "model_assisted", "prompt": "Is it gory?", "on_uncertain": on_uncertain}
    )


def _turn(
    turn: int, beat: int, text: str, *, target: Target = Target.TEXT_OUTPUT, **kw: Any
) -> HistoryRecord:
    return HistoryRecord(
        f"e-{turn}-{beat}", {"turn": turn, "variant": 1, "beat": beat}, target, text, **kw
    )


class Model:
    """A scripted model check: answers by substring, and counts its calls."""

    def __init__(self, answers: Mapping[str, bool | None]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, text: str, policy: Policy, target: Target) -> bool | None:
        self.calls.append((text, policy.id))
        for needle, answer in self.answers.items():
            if needle in text:
                return answer
        return False


async def test_every_violation_is_listed_in_position_order() -> None:
    """Every record breaking every policy is listed (a deterministic violation does not end
    the check), turns first by turn and beat, then pinned memories and instructions; only
    positions and policy identifiers, never the text. Complement: a clean record adds
    nothing, and a policy applying only to inputs never sees a reply."""
    dragons = _policy("p-dragons", _words("dragon"))
    knives = _policy("p-knives", _words("knife"), applies_to=(Target.TEXT_INPUT,))
    records = [
        HistoryRecord("pin:1", {"kind": "pin", "id": "1"}, Target.TEXT_INPUT, "a dragon pin"),
        _turn(2, 2, "A dragon and a knife."),
        _turn(1, 1, "I draw a knife.", target=Target.TEXT_INPUT),
        _turn(1, 2, "A dragon lands."),
        _turn(3, 2, "A cat purrs."),
    ]
    result = await evaluate_history([dragons, knives], records)
    assert [(v.position, v.policy_id) for v in result.violations] == [
        ({"turn": 1, "variant": 1, "beat": 1}, "p-knives"),
        ({"turn": 1, "variant": 1, "beat": 2}, "p-dragons"),
        ({"turn": 2, "variant": 1, "beat": 2}, "p-dragons"),
        ({"kind": "pin", "id": "1"}, "p-dragons"),
    ]
    assert result.checked == 5
    assert not result.passed
    listed = str([v.as_json() for v in result.violations])
    assert all(record.text not in listed for record in records)
    assert result.violations[0].as_json() == {
        "position": {"turn": 1, "variant": 1, "beat": 1},
        "policy_id": "p-knives",
        "policy_name": "name of p-knives",
    }


async def test_character_policies_apply_to_their_character_only() -> None:
    """A character-scoped policy checks that character's records only (complement: the same
    text by another character passes)."""
    ada = _policy(
        "p-ada",
        EntityListRule(type="entity_list", names=("Brann",)),
        scope=Scope.CHARACTER,
        scope_id="c-ada",
    )
    records = [
        _turn(1, 2, "Brann waves.", character_ids=("c-ada",)),
        _turn(2, 2, "Brann waves.", character_ids=("c-wren",)),
    ]
    result = await evaluate_history([ada], records)
    assert [v.position["turn"] for v in result.violations] == [1]


async def test_model_assisted_policies_within_the_per_record_budget() -> None:
    """Each model-assisted policy is one call per record, at most ``calls_per_record`` per
    record; one the budget cannot afford, or an uncertain answer, follows ``on_uncertain``
    (not stored). Conclusive verdicts are returned to be cached."""
    gore = _policy("p-gore", _assisted())
    lenient = _policy("p-lenient", _assisted("pass"))
    model = Model({"blood": True, "maybe": None})
    records = [_turn(1, 2, "blood everywhere"), _turn(2, 2, "maybe"), _turn(3, 2, "calm")]
    result = await evaluate_history([gore, lenient], records, model_check=model, calls_per_record=1)
    # One call per record (the first policy); the second is unaffordable: on_uncertain.
    assert [pid for _, pid in model.calls] == ["p-gore", "p-gore", "p-gore"]
    assert [(v.position["turn"], v.policy_id) for v in result.violations] == [
        (1, "p-gore"),
        (2, "p-gore"),
    ]
    assert result.verdicts == {
        ("e-1-2", "p-gore", 1): "violation",
        ("e-3-2", "p-gore", 1): "pass",
    }
    assert result.model_calls == 3

    # Complement: a budget for both asks the lenient policy too, and passes on its uncertainty.
    model = Model({"maybe": None})
    roomy = await evaluate_history([gore, lenient], records, model_check=model, calls_per_record=2)
    assert len(model.calls) == 6
    assert [(v.position["turn"], v.policy_id) for v in roomy.violations] == [(2, "p-gore")]


async def test_cached_verdicts_are_not_asked_again() -> None:
    """A verdict cached for the record and the policy's version is reused, deterministic or
    not; a new version of the policy is checked again."""
    gore = _policy("p-gore", _assisted())
    dragons = _policy("p-dragons", _words("dragon"))
    records = [_turn(1, 2, "A dragon bleeds.")]
    cache: dict[CacheKey, str] = {
        ("e-1-2", "p-gore", 1): "pass",
        ("e-1-2", "p-dragons", 1): "violation",
    }
    model = Model({"bleeds": True})
    result = await evaluate_history(
        [gore, dragons], records, model_check=model, calls_per_record=2, cache=cache
    )
    assert model.calls == []
    assert result.cached == 2
    assert [v.policy_id for v in result.violations] == ["p-dragons"]

    newer = _policy("p-gore", _assisted(), version=2)
    again = await evaluate_history(
        [newer, dragons], records, model_check=model, calls_per_record=2, cache=cache
    )
    assert [pid for _, pid in model.calls] == ["p-gore"]
    assert {v.policy_id for v in again.violations} == {"p-gore", "p-dragons"}


async def test_an_image_without_a_checker_counts_against_the_change() -> None:
    """A story image (its prompt checked like a text) with no image checker configured is a
    violation coded ``image_unchecked``; with a checker that passes it, only its prompt counts.
    M3's story-image hook answers no images."""
    prompts = _policy("p-prompt", _words("dragon"), applies_to=(Target.IMAGE_PROMPT,))
    output = _policy("p-out", _assisted(), applies_to=(Target.IMAGE_OUTPUT,))
    image = HistoryRecord(
        "img-1", {"kind": "image", "id": "img-1"}, Target.IMAGE_PROMPT, "a calm sea", image=True
    )
    result = await evaluate_history([prompts, output], [image])
    assert [v.as_json() for v in result.violations] == [
        {
            "position": {"kind": "image", "id": "img-1"},
            "policy_id": "p-out",
            "policy_name": "name of p-out",
            "code": IMAGE_UNCHECKED,
        }
    ]

    async def passes(record: HistoryRecord) -> bool | None:
        return False

    checked = await evaluate_history([prompts, output], [image], image_check=passes)
    assert checked.passed
    assert await story_image_records(cast(AsyncConnection, None), "s-1") == []


async def test_progress_after_each_record() -> None:
    seen: list[tuple[int, int, int]] = []

    async def progress(done: int, total: int, found: Mapping[CacheKey, str]) -> None:
        seen.append((done, total, len(found)))

    dragons = _policy("p-dragons", _words("dragon"))
    await evaluate_history(
        [dragons], [_turn(1, 2, "dragon"), _turn(2, 2, "cat")], progress=progress
    )
    assert seen == [(1, 2, 1), (2, 2, 1)]
    empty = await evaluate_history([dragons], [], progress=progress)
    assert empty.passed
    assert empty.checked == 0


def test_positions_sort_turns_before_other_records() -> None:
    positions: list[dict[str, Any]] = [
        {"kind": "instruction", "id": "custom_instructions"},
        {"kind": "pin", "id": "b"},
        {"turn": 2, "variant": 1, "beat": 1},
        {"kind": "seed", "id": "z"},
        {"turn": 1, "variant": 2, "beat": 2},
    ]
    keys = sorted(positions, key=position_key)
    assert keys == [
        {"turn": 1, "variant": 2, "beat": 2},
        {"turn": 2, "variant": 1, "beat": 1},
        {"kind": "seed", "id": "z"},
        {"kind": "pin", "id": "b"},
        {"kind": "instruction", "id": "custom_instructions"},
    ]


def test_stamp_groups_policies_by_scope() -> None:
    found = [
        _policy("p-g", _words("x"), scope=Scope.GLOBAL, scope_id=None, version=3),
        _policy("p-r", _words("y")),
        _policy("p-c", _words("z"), scope=Scope.CHARACTER, scope_id="c-1", version=2),
    ]
    assert stamp_of("r-teen", "v-teen", found) == {
        "rating_id": "r-teen",
        "rating_version_id": "v-teen",
        "global": {"p-g": 3},
        "rating": {"p-r": 1},
        "story": {},
        "character": {"p-c": 2},
    }


@pytest.mark.parametrize("on_uncertain", ["violation", "pass"])
async def test_no_model_follows_on_uncertain(on_uncertain: str) -> None:
    gore = _policy("p-gore", _assisted(on_uncertain))
    result = await evaluate_history([gore], [_turn(1, 2, "blood")])
    assert result.passed is (on_uncertain == "pass")
    assert result.verdicts == {}
