"""What Core asks of the Orchestrator, without importing it (docs/02 layering; #103).

Core is the deterministic layer and never imports ``epistrel.orchestrator`` (import-linter
contract ``core-does-not-import-orchestrator-or-api``). A core module that needs the input gate,
a policy evaluation, a model call or a background job takes it as one of these ports; ``Engine``
supplies them at construction (``Ports``), and the Orchestrator implements them.

Until the story that first needs a port wires the Orchestrator's implementation into ``Engine``
(#111, #118, #119, #120, #121, #122, #124), the defaults do nothing: the input gate passes every
text, the policy evaluator reports no hit, the model caller answers ``None`` (no model) and the
job starter starts nothing (``None``). Nothing in Core calls a port yet, so no default decides
anything today; the story that adds a caller wires the real port and proves a refusal with it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True, slots=True)
class PortText:
    """One text a port checks: where it sits (``definition.personality``,
    ``roles[0].setup_description``) and the text."""

    locator: str
    text: str


@dataclass(frozen=True, slots=True)
class PolicyHit:
    """One text that breaks one policy: the text's locator and the policy."""

    locator: str
    policy_id: str
    name: str
    message: str


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
        self, texts: Sequence[PortText], *, rating_id: str | None = None
    ) -> Sequence[PolicyHit]:
        """Every (text, policy) hit."""
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
    """The default policy evaluator: no policy, so no hit."""

    async def evaluate(
        self, texts: Sequence[PortText], *, rating_id: str | None = None
    ) -> Sequence[PolicyHit]:
        return ()


class NoModel:
    """The default model caller: no model answers."""

    async def complete(self, purpose: str, prompt: str) -> str | None:
        return None


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
    """The four ports an ``Engine`` hands to Core (each defaults to its no-op)."""

    input_gate: InputGate = field(default_factory=PassingGate)
    policy_evaluator: PolicyEvaluator = field(default_factory=NoPolicies)
    model_caller: ModelCaller = field(default_factory=NoModel)
    job_starter: JobStarter = field(default_factory=NoJobs)


__all__ = [
    "InputGate",
    "JobStarter",
    "ModelCaller",
    "NoJobs",
    "NoModel",
    "NoPolicies",
    "PassingGate",
    "PolicyEvaluator",
    "PolicyHit",
    "PortText",
    "Ports",
]
