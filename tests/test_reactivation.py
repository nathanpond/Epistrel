"""Which turns a reactivation re-checks (FR-RATE-7; #120): the stamp comparison, without a
database."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from epistrel.core.policies.model import Policy
from epistrel.core.ratings.reactivation import held_policies, missing, policy_key


def _policy(policy_id: str, version: int) -> Policy:
    """Only a policy's id and version are compared."""
    return cast(Policy, SimpleNamespace(id=policy_id, version=version))


def _stamp(**scopes: dict[str, int]) -> dict[str, Any]:
    return {
        "rating_id": "R",
        "rating_version_id": "V1",
        "global": {},
        "rating": {},
        "story": {},
        "character": {},
        **scopes,
    }


def test_a_null_stamp_lacks_every_policy() -> None:
    current = (_policy("A", 1), _policy("G", 2))
    assert missing(None, current) == current
    assert missing(None, ()) == ()
    assert held_policies(None) == {}


def test_a_stamp_lacking_a_policy_or_holding_an_older_version_is_unchecked() -> None:
    a, g = _policy("A", 1), _policy("G", 2)
    assert missing(_stamp(rating={"A": 1}, **{"global": {"G": 2}}), (a, g)) == ()
    assert missing(_stamp(rating={"A": 1}), (a, g)) == (g,)
    assert missing(_stamp(rating={"A": 1}, **{"global": {"G": 1}}), (a, g)) == (g,)


def test_only_policies_count_not_the_rating_version_or_extra_policies() -> None:
    a = _policy("A", 3)
    edited = {**_stamp(rating={"A": 3, "OLD": 1}), "rating_version_id": "V9"}
    assert missing(edited, (a,)) == ()
    assert policy_key((a,)) == frozenset({("A", 3)})
