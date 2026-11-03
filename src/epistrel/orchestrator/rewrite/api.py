"""The request and response shapes of the history side-channel (``/stories/{id}/rewrites``;
FR-REWRITE-1,2,4,7; #223). The session's stored columns are ``core.tables.rewrites``'."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from epistrel.core.trimmed import trim_as_sent

INSTRUCTION_MAX = 4_000
ANSWER_TEXT_MAX = 2_000

Severity = Literal["high", "low"]
Instruction = Annotated[
    str,
    StringConstraints(min_length=1, max_length=INSTRUCTION_MAX, strip_whitespace=True),
    BeforeValidator(trim_as_sent(INSTRUCTION_MAX)),
]


class ThresholdMode(BaseModel):
    """``{"threshold": "high" | "low"}``: ask only about conflicts at or above the severity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold: Severity


ResolutionMode = Literal["synchronous", "best_effort"] | ThresholdMode
PreviewMode = Literal["preview_first", "apply_directly"]


class RewriteRequest(BaseModel):
    """``POST /stories/{id}/rewrites``: an instruction about the story's past, how conflicts
    resolve (``synchronous`` by default) and whether to preview first (the default)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "instruction": "Bobby actually went to UCLA.",
                    "resolution_mode": "synchronous",
                    "preview": "preview_first",
                }
            ]
        },
    )

    instruction: Instruction
    resolution_mode: ResolutionMode = "synchronous"
    preview: PreviewMode = "preview_first"

    def mode(self) -> tuple[str, str | None]:
        """The stored ``(resolution_mode, threshold_severity)``."""
        if isinstance(self.resolution_mode, ThresholdMode):
            return "threshold", self.resolution_mode.threshold
        return str(self.resolution_mode), None


class AnswerItem(BaseModel):
    """One answer: an option of the question, free text, or the recommended option."""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=26, max_length=26)
    option_id: str | None = Field(default=None, max_length=40)
    text: Annotated[str | None, StringConstraints(min_length=1, max_length=ANSWER_TEXT_MAX)] = None
    accept_recommended: StrictBool = False

    @model_validator(mode="after")
    def _one(self) -> AnswerItem:
        given = [self.option_id is not None, self.text is not None, self.accept_recommended]
        if sum(given) != 1:
            raise ValueError("an answer gives exactly one of option_id, text, accept_recommended")
        return self


class AnswerRequest(BaseModel):
    """``POST /stories/{id}/rewrites/{rid}/answers``: answers to the session's questions,
    validated all together."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"answers": [{"question_id": "01JQ0000000000000000000000", "option_id": "o1"}]}
            ]
        },
    )

    answers: list[AnswerItem] = Field(min_length=1, max_length=50)


class QuestionOption(BaseModel):
    """One option of a question: its id (``o1``…), label and what choosing it does."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    effect: dict[str, Any] = Field(default_factory=dict)


class RewriteQuestion(BaseModel):
    """A question the session asks the author, always with a recommended option and the reason
    for it (owner decision 4, 2026-10-10)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str
    prompt: str
    options: list[QuestionOption]
    recommended_option_id: str
    recommendation_reason: str
    severity: Severity | None = None
    required: bool = True
    answer_option_id: str | None = None
    answer_text: str | None = None
    accepted_recommendation: bool | None = None
    answered_by: str | None = None
    answered_at: datetime | None = None


class BlastRadius(BaseModel):
    """What a plan touches (FR-DIRECT-3): incidental mentions, cascaded justifications and
    irreducible conflicts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incidental: list[dict[str, Any]] = Field(default_factory=list)
    cascaded: list[dict[str, Any]] = Field(default_factory=list)
    irreducible: list[dict[str, Any]] = Field(default_factory=list)


class Preview(BaseModel):
    """A session's plan (FR-REWRITE-4): the ``timeline_revision`` it was computed against, its
    hash (identical across modes, FR-REWRITE-5), the normalized changes and their blast
    radius."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_revision: int
    plan_hash: str
    changes: list[dict[str, Any]] = Field(default_factory=list)
    blast_radius: BlastRadius = Field(default_factory=BlastRadius)
    blast_radius_truncated: bool = False
    new_entities: list[dict[str, Any]] = Field(default_factory=list)
    relabels: list[dict[str, Any]] = Field(default_factory=list)
    refused_changes: list[dict[str, Any]] = Field(default_factory=list)
    notifications: list[dict[str, Any]] = Field(default_factory=list)
    timing_uncertain: list[dict[str, Any]] = Field(default_factory=list)
    suggested_ports: list[dict[str, Any]] = Field(default_factory=list)


class RewriteSession(BaseModel):
    """A rewrite session as the author sees it: its instruction, the modes it runs with, its
    state, its preview (``null`` while a question blocks the plan) and its questions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    story_id: str
    kind: str
    state: str
    instruction: str | None
    resolution_mode: ResolutionMode
    preview_mode: PreviewMode
    pipeline: Literal["structured", "lite"]
    principal: str
    target_amendment_id: str | None = None
    """An ``undo`` or ``redo`` session's amendment (#238)."""
    base_revision: int | None = None
    preview: Preview | None = None
    questions: list[RewriteQuestion] = Field(default_factory=list)
    stale_count: int = 0
    result_amendment_id: str | None = None
    report: dict[str, Any] | None = None
    """An applied session's report (#224): its resolutions, what it applied, its
    notifications."""
    last_error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class RewriteSummary(BaseModel):
    """A session in a list: no preview, no questions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: str
    state: str
    instruction: str | None
    open_questions: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class RewriteList(BaseModel):
    """``GET /stories/{id}/rewrites``: newest first, a page at a time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[RewriteSummary]
    next_cursor: str | None = None


class MergeRequest(BaseModel):
    """``POST /stories/{id}/entities/merge``: entities the author reveals to be one referent
    (#225, FR-PROP-7): the first is the survivor, each other ``same_as`` it; the modes as a
    rewrite's; ``identity_proposal_id`` the #171 proposal it settles, when there is one."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "entity_ids": ["01JBOBBY000000000000000000", "01JSTRANGER000000000000000"],
                    "resolution_mode": "synchronous",
                    "preview": "preview_first",
                }
            ]
        },
    )

    entity_ids: list[Annotated[str, StringConstraints(min_length=26, max_length=26)]] = Field(
        min_length=1, max_length=10
    )
    visibility: Literal["objective"] = "objective"
    resolution_mode: ResolutionMode = "synchronous"
    preview: PreviewMode = "preview_first"
    identity_proposal_id: str | None = Field(default=None, min_length=26, max_length=26)

    def mode(self) -> tuple[str, str | None]:
        if isinstance(self.resolution_mode, ThresholdMode):
            return "threshold", self.resolution_mode.threshold
        return str(self.resolution_mode), None


class AmendmentSessionRequest(BaseModel):
    """``POST /stories/{id}/amendments/{aid}/undo`` and ``…/redo`` (#238): the modes, as a
    rewrite's."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"resolution_mode": "best_effort", "preview": "preview_first"}]
        },
    )

    resolution_mode: ResolutionMode = "synchronous"
    preview: PreviewMode = "preview_first"

    def mode(self) -> tuple[str, str | None]:
        if isinstance(self.resolution_mode, ThresholdMode):
            return "threshold", self.resolution_mode.threshold
        return str(self.resolution_mode), None


class ApplyRequest(BaseModel):
    """``POST /stories/{id}/rewrites/{rid}/apply``: the revision the preview was computed
    against (required; it must be the story's current one)."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"base_revision": 7}]}
    )

    base_revision: StrictInt | None = Field(default=None, ge=0)


class Applied(BaseModel):
    """An apply's answer (#224): the amendment it recorded, its report and the story's new
    revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    amendment_id: str
    report: dict[str, Any]
    timeline_revision: int


@dataclass(frozen=True, slots=True)
class RewriteWrite:
    """A session write's answer (create, answers, cancel, apply): the session, the story's
    ``timeline_revision`` (only an apply moves it, FR-REWRITE-7) and, when the call applied,
    the apply."""

    session: RewriteSession
    timeline_revision: int = 0
    apply: Applied | None = None
    replayed: bool = False


__all__ = [
    "ANSWER_TEXT_MAX",
    "INSTRUCTION_MAX",
    "AmendmentSessionRequest",
    "AnswerItem",
    "AnswerRequest",
    "Applied",
    "ApplyRequest",
    "BlastRadius",
    "MergeRequest",
    "Preview",
    "PreviewMode",
    "QuestionOption",
    "ResolutionMode",
    "RewriteList",
    "RewriteQuestion",
    "RewriteRequest",
    "RewriteSession",
    "RewriteSummary",
    "RewriteWrite",
    "Severity",
    "ThresholdMode",
]
