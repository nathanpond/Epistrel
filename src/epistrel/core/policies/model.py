"""The policy model (FR-GUARD-1,2,8,9): scopes, targets, rule kinds, and a policy's shape.

A policy belongs to one **scope** — ``global``, ``rating`` (``scope_id`` = the rating),
``story`` or ``character`` (``story_id`` set for both, ``scope_id`` = the story or the
character) — and applies to some of four **targets**. Its ``rule`` is deterministic
(``wordlist``, ``regex``, ``entity_list``) or ``model_assisted`` (a prompt evaluated through the
``guardrail check`` purpose by #56/#68); the policy's ``kind`` follows from its rule, so the
model-call cost of a policy set is visible (FR-GUARD-9). Its only ``effect`` is ``deny``: a
lower scope can add policies, never relax one (FR-GUARD-8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, field_validator

NAME_MAX = 80
MESSAGE_MAX = 500
WORDS_MAX = 2_000
WORD_MAX = 200
REGEXES_MAX = 20
REGEX_MAX = 500
PROMPT_MAX = 4_000


class Scope(StrEnum):
    """Where a policy applies: the deployment, a rating, a story, or one character."""

    GLOBAL = "global"
    RATING = "rating"
    STORY = "story"
    CHARACTER = "character"


SCOPE_RANK = {Scope.GLOBAL: 0, Scope.RATING: 1, Scope.STORY: 2, Scope.CHARACTER: 3}
"""Higher scopes first: a policy can never be relaxed by one of a larger rank."""

STORY_SCOPES = frozenset({Scope.STORY, Scope.CHARACTER})


class Target(StrEnum):
    """What a policy applies to (FR-GUARD-8, FR-GUARD-10, FR-IMG-3)."""

    TEXT_INPUT = "text_input"
    TEXT_OUTPUT = "text_output"
    IMAGE_PROMPT = "image_prompt"
    IMAGE_OUTPUT = "image_output"


def default_targets(scope: Scope) -> tuple[Target, ...]:
    """docs/02 §4.12: global policies apply to text input and output, the others to output."""
    if scope is Scope.GLOBAL:
        return (Target.TEXT_INPUT, Target.TEXT_OUTPUT)
    return (Target.TEXT_OUTPUT,)


class PolicyKind(StrEnum):
    """``deterministic`` rules cost nothing; ``model_assisted`` ones cost a ``guardrail_check``
    model call per check (FR-GUARD-9)."""

    DETERMINISTIC = "deterministic"
    MODEL_ASSISTED = "model_assisted"


class Effect(StrEnum):
    """The only effect a policy can have. ``allow``/``exempt`` would relax: refused."""

    DENY = "deny"


AgeCondition = Literal["minor_involved_or_present"]
"""FR-GUARD-11's condition on true ages, enforced by ``orchestrator.policies.age`` (#55): the
policy applies only when a character younger than ``minor_age_threshold`` is involved in or
present for the content. M2 accepts this one value; narrower conditions need presence (M5)."""

Word = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=WORD_MAX)]
Pattern = Annotated[str, StringConstraints(min_length=1, max_length=REGEX_MAX)]


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WordlistRule(_Rule):
    """Whole words and phrases, compared after NFKC normalisation and case folding."""

    type: Literal["wordlist"]
    words: tuple[Word, ...] = Field(min_length=1, max_length=WORDS_MAX)


class RegexRule(_Rule):
    """Python ``re`` patterns searched anywhere, ignoring case, each under a deadline."""

    type: Literal["regex"]
    patterns: tuple[Pattern, ...] = Field(min_length=1, max_length=REGEXES_MAX)

    @field_validator("patterns")
    @classmethod
    def _compiles(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for index, pattern in enumerate(patterns):
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"pattern {index} does not compile: {exc.msg}") from None
        return patterns


class EntityListRule(_Rule):
    """Named entities; any whole-word mention matches, under the wordlist rules.

    At story and character scope an empty ``names`` means "the story's characters": the names
    are frozen into the rule when it is saved (the whole cast for a story policy, the character
    itself for a character policy, #50). Every stored rule has names.
    """

    type: Literal["entity_list"]
    names: tuple[Word, ...] = Field(default=(), max_length=WORDS_MAX)


class ModelAssistedRule(_Rule):
    """A check the ``guardrail check`` model purpose evaluates (#56/#68); ``evaluate`` skips it."""

    type: Literal["model_assisted"]
    prompt: str = Field(min_length=1, max_length=PROMPT_MAX)
    on_uncertain: Literal["violation", "pass"] = "violation"


Rule = Annotated[
    WordlistRule | RegexRule | EntityListRule | ModelAssistedRule, Field(discriminator="type")
]
RULE_ADAPTER: TypeAdapter[Rule] = TypeAdapter(Rule)


def kind_of(rule: Rule) -> PolicyKind:
    if isinstance(rule, ModelAssistedRule):
        return PolicyKind.MODEL_ASSISTED
    return PolicyKind.DETERMINISTIC


@dataclass(frozen=True, slots=True)
class Policy:
    """One stored policy at its current version."""

    id: str
    scope: Scope
    scope_id: str | None
    story_id: str | None
    name: str
    applies_to: tuple[Target, ...]
    kind: PolicyKind
    effect: Effect
    rule: Rule
    age_condition: str | None
    message: str
    version: int
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
    retired_at: datetime | None


@dataclass(frozen=True, slots=True)
class Violation:
    """A policy a text breaks. No spans: the caller already holds the text (#49)."""

    policy_id: str
    name: str
    message: str
    kind: PolicyKind
