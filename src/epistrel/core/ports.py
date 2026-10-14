"""What Core asks of the Orchestrator, without importing it (docs/02 layering; #103).

Core is the deterministic layer and never imports ``epistrel.orchestrator`` (import-linter
contract ``core-does-not-import-orchestrator-or-api``). A core module that needs the input gate,
a policy evaluation, a model call or a background job takes it as one of these ports; ``Engine``
supplies them at construction (``Ports``), and the Orchestrator implements them.

Until the story that first needs a port wires the Orchestrator's implementation into ``Engine``
(#118, #119, #120, #121, #122, #124), the defaults do nothing: the input gate passes every
text, the model caller answers ``None`` (no model), the job starter starts nothing (``None``)
and the summary compressor (#124) asks no model, so an erasure redacts instead of
regenerating. The policy evaluator is the first port wired (#111: the catalog validation's input
policies; the Orchestrator's ``CatalogPolicyCheck``); its default evaluates nothing and says so
(``unavailable``), so a validation run without the Engine lists the check rather than passing
it silently.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@dataclass(frozen=True, slots=True)
class PortText:
    """One text a port checks: where it sits (``definition.personality``,
    ``roles[0].setup_description``) and the text."""

    locator: str
    text: str


@dataclass(frozen=True, slots=True)
class PolicyHit:
    """One text that breaks one policy: the text's locator, the policy, and the span of the text
    that matched (a deterministic word or name list's match; ``None`` when no span can be
    located: a pattern or a model-assisted policy)."""

    locator: str
    policy_id: str
    name: str
    message: str
    span: str | None = None


POLICIES_UNAVAILABLE = "input_policy"
"""What ``NoPolicies`` lists as unavailable: no policy could be evaluated."""


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    """What a policy evaluation found: every (text, policy) hit, the names of what it could not
    evaluate (``age_conditioned_policies``, ``input_policy_model_assisted``, or ``input_policy``
    when nothing could be), and the model calls it made."""

    hits: tuple[PolicyHit, ...] = ()
    unavailable: tuple[str, ...] = ()
    model_calls: int = 0


class InputGate(Protocol):
    """The input gate (FR-GUARD-10): refuses texts that break an input policy."""

    async def enforce(
        self,
        texts: Sequence[PortText],
        *,
        entry_point: str,
        principal: str,
        rating_id: str | None = None,
    ) -> None:
        """Return when every text passes; raise ``GuardrailViolation`` (writing its audit row)
        when one breaks a policy."""
        ...


class PolicyEvaluator(Protocol):
    """Policy evaluation without refusal or audit (a publish-time report, a re-check)."""

    async def evaluate(
        self,
        db: AsyncEngine,
        texts: Sequence[PortText],
        *,
        rating_id: str | None = None,
        max_model_calls: int = 0,
    ) -> PolicyEvaluation:
        """Every (text, policy) hit of the live global input policies and ``rating_id``'s
        input policies, with model-assisted policies checked within ``max_model_calls``; what
        could not be evaluated is listed, never passed. Writes nothing."""
        ...


class ModelCaller(Protocol):
    """One model call for a purpose's configuration (FR-MODEL-1)."""

    async def complete(self, purpose: str, prompt: str) -> str | None:
        """The model's text, or ``None`` when no model can answer (the caller degrades)."""
        ...


class JobStarter(Protocol):
    """Background work: a job enqueued in the caller's transaction (#70)."""

    async def start(
        self,
        conn: AsyncConnection,
        kind: str,
        payload: Mapping[str, Any],
        *,
        story_id: str | None = None,
    ) -> str | None:
        """The new job's id, or ``None`` when nothing was started."""
        ...


@dataclass(frozen=True, slots=True)
class SummaryWindow:
    """One Lite rolling-summary segment erasure regenerates (#124): its event (``segment_id``)
    and the turns it covers, with the label of the included segment just before it (context
    only)."""

    segment_id: str
    covers_from: int
    covers_to: int
    prior: str | None = None


@dataclass(frozen=True, slots=True)
class RegeneratedText:
    """A window's new summary text, the model that wrote it and its prompt's hash."""

    text: str
    model: str | None
    prompt_hash: str | None


@dataclass(frozen=True, slots=True)
class Regeneration:
    """What a regeneration run made: the new text of each window it could summarise (by
    ``segment_id``), whether it ran out of calls (``capped``) and whether a call failed or no
    model could answer (``failed``)."""

    texts: Mapping[str, RegeneratedText] = field(default_factory=dict)
    capped: bool = False
    failed: bool = False


class SummaryCompressor(Protocol):
    """#72's compression over a given window (#124's summary regeneration): each window's
    transcript rows, read as they are now, summarised through the ``lite_summary`` purpose under
    one run's budget (``core.summary.MAX_CALLS_PER_RUN``), each text checked against the
    story's deterministic output policies."""

    @property
    def available(self) -> bool:
        """Whether a model can be asked at all (``False``: erasure redacts instead)."""
        ...

    async def regenerate(
        self, db: AsyncEngine, story_id: str, windows: Sequence[SummaryWindow]
    ) -> Regeneration:
        """The new texts, oldest window first; never raises for a model failure."""
        ...


class PassingGate:
    """The default input gate: no policy is evaluated, every text passes."""

    async def enforce(
        self,
        texts: Sequence[PortText],
        *,
        entry_point: str,
        principal: str,
        rating_id: str | None = None,
    ) -> None:
        return None


class NoPolicies:
    """The default policy evaluator: nothing is evaluated, and the evaluation says so (it lists
    ``input_policy`` as unavailable; a check never passes silently)."""

    async def evaluate(
        self,
        db: AsyncEngine,
        texts: Sequence[PortText],
        *,
        rating_id: str | None = None,
        max_model_calls: int = 0,
    ) -> PolicyEvaluation:
        return PolicyEvaluation(unavailable=(POLICIES_UNAVAILABLE,))


class NoModel:
    """The default model caller: no model answers."""

    async def complete(self, purpose: str, prompt: str) -> str | None:
        return None


class NoCompressor:
    """The default summary compressor: no model is asked (erasure's redaction stands)."""

    @property
    def available(self) -> bool:
        return False

    async def regenerate(
        self, db: AsyncEngine, story_id: str, windows: Sequence[SummaryWindow]
    ) -> Regeneration:
        return Regeneration(failed=bool(windows))


class NoJobs:
    """The default job starter: nothing is started."""

    async def start(
        self,
        conn: AsyncConnection,
        kind: str,
        payload: Mapping[str, Any],
        *,
        story_id: str | None = None,
    ) -> str | None:
        return None


@dataclass(frozen=True, slots=True)
class Ports:
    """The ports an ``Engine`` hands to Core (each defaults to its no-op): the four #103 named
    and #124's summary compressor."""

    input_gate: InputGate = field(default_factory=PassingGate)
    policy_evaluator: PolicyEvaluator = field(default_factory=NoPolicies)
    model_caller: ModelCaller = field(default_factory=NoModel)
    job_starter: JobStarter = field(default_factory=NoJobs)
    summary_compressor: SummaryCompressor = field(default_factory=NoCompressor)


__all__ = [
    "POLICIES_UNAVAILABLE",
    "InputGate",
    "JobStarter",
    "ModelCaller",
    "NoCompressor",
    "NoJobs",
    "NoModel",
    "NoPolicies",
    "PassingGate",
    "PolicyEvaluation",
    "PolicyEvaluator",
    "PolicyHit",
    "PortText",
    "Ports",
    "RegeneratedText",
    "Regeneration",
    "SummaryCompressor",
    "SummaryWindow",
]
