"""Reading ``policies`` rows into ``Policy`` values (shared by the store's read path and its
single write path, ``epistrel.orchestrator.policies``)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, select

from epistrel.core.policies.model import (
    RULE_ADAPTER,
    Effect,
    Policy,
    PolicyKind,
    Rule,
    Scope,
    Target,
)
from epistrel.core.tables import policies


def select_policies() -> Select[Any]:
    return select(policies)


def policy_of(row: Any) -> Policy:
    mapping = row._mapping
    return Policy(
        id=mapping["id"],
        scope=Scope(mapping["scope"]),
        scope_id=mapping["scope_id"],
        story_id=mapping["story_id"],
        name=mapping["name"],
        applies_to=tuple(Target(t) for t in mapping["applies_to"]),
        kind=PolicyKind(mapping["kind"]),
        effect=Effect(mapping["effect"]),
        rule=RULE_ADAPTER.validate_python(mapping["rule"]),
        age_condition=mapping["age_condition"],
        message=mapping["message"],
        version=int(mapping["version"]),
        created_by=mapping["created_by"],
        updated_by=mapping["updated_by"],
        created_at=mapping["created_at"],
        updated_at=mapping["updated_at"],
        retired_at=mapping["retired_at"],
    )


def rule_json(rule: Rule) -> dict[str, Any]:
    """The stored form of a rule (also what ``config_changes`` records)."""
    dumped: dict[str, Any] = RULE_ADAPTER.dump_python(rule, mode="json")
    return dumped


def content_of(policy: Policy) -> dict[str, Any]:
    """The editable content of a policy, as ``config_changes`` records it."""
    return {
        "scope": policy.scope.value,
        "scope_id": policy.scope_id,
        "name": policy.name,
        "applies_to": [t.value for t in policy.applies_to],
        "kind": policy.kind.value,
        "effect": policy.effect.value,
        "rule": rule_json(policy.rule),
        "age_condition": policy.age_condition,
        "message": policy.message,
        "version": policy.version,
    }
