"""What callers send to the policy store's write path (``/admin/policies`` and #63's story
settings). The bodies are parsed here; ``admin`` applies the scope rules.

``effect`` is accepted as any string so that an attempt to relax (``allow``, ``exempt``) is
answered with ``cannot_relax`` rather than a generic validation error (FR-GUARD-8).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from epistrel.core.policies.model import MESSAGE_MAX, NAME_MAX, AgeCondition, Effect, Rule, Target
from epistrel.core.trimmed import raw_max_length

POLICY_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"

PolicyId = Annotated[str, StringConstraints(pattern=POLICY_ID_PATTERN)]
Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX),
    BeforeValidator(raw_max_length(NAME_MAX)),
]
Message = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MESSAGE_MAX),
    BeforeValidator(raw_max_length(MESSAGE_MAX)),
]
Targets = Annotated[list[Target], Field(min_length=1)]


class PolicyContent(BaseModel):
    """The editable part of a policy. ``applies_to`` defaults by scope (docs/02 §4.12)."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    applies_to: Targets | None = None
    rule: Rule
    effect: str = Effect.DENY.value
    age_condition: AgeCondition | None = None
    message: Message


class PolicyFields(PolicyContent):
    """``POST /admin/policies``: a global policy, or a rating policy (``scope_id`` = rating).

    ``id`` is a client slug or, when omitted, a new ULID.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "No real addresses",
                    "scope": "global",
                    "applies_to": ["text_output"],
                    "rule": {"type": "regex", "patterns": ["\\d+ [A-Z][a-z]+ (Street|Road)"]},
                    "message": "Do not give real-looking street addresses.",
                }
            ]
        }
    )

    id: PolicyId | None = None
    scope: Literal["global", "rating"]
    scope_id: str | None = None


class PolicyReplacement(PolicyContent):
    """``PUT /admin/policies/{id}``: a full replacement. Scope and id never change, and
    ``applies_to`` and ``age_condition`` are required so a forgotten field cannot clear them."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "No real addresses",
                    "applies_to": ["text_output"],
                    "rule": {"type": "regex", "patterns": ["\\d+ [A-Z][a-z]+ (Street|Road)"]},
                    "age_condition": "minor_involved_or_present",
                    "message": "Do not give real-looking street addresses.",
                }
            ]
        }
    )

    applies_to: Targets
    age_condition: AgeCondition | None


class StoryPolicyItem(PolicyContent):
    """One item of a story's ``policies`` upsert list (#63's ``PATCH /stories/{id}/settings``).

    A ``story`` policy's ``scope_id`` is the story; a ``character`` policy names its character.
    An item with the id of an existing policy of this story edits it; any other id creates.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "No talk of the war",
                    "scope": "story",
                    "rule": {"type": "wordlist", "words": ["the war"]},
                    "message": "The war is never mentioned.",
                }
            ]
        }
    )

    id: PolicyId | None = None
    scope: Literal["story", "character"] = "story"
    scope_id: str | None = None
