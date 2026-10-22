"""The eval harness's optional second judge model (#475).

By default the harness's judge calls go to the run's own model: E-03's entailment judgement of
the reflections the model wrote, and E-12/E-13's ``quality_judge``. A model judging its own
output may be lenient with it, so a run may name a **judge model** (``--judge-model``, with
``--judge-base-url`` and ``--judge-api-key`` defaulting to the run's). Its calls go through
``JudgeModels``, a Model Access Layer of their own. It resolves each purpose as the deployment
does (params, timeout), then swaps in the judge's model, endpoint and key. The engine's own
pipeline calls (turns, the verifier, consolidation, every job) never use it: they keep the run
model through ``Engine.models``.

On the stub, ``JudgeModels`` has a stub adapter of its own, which reports the judge's model
name, so a test can tell the judge's calls from the run's (``StubAdapter.calls``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.config import EffectiveConfig


@dataclass(frozen=True, slots=True)
class JudgeEndpoint:
    """The judge model and where it is served. ``None`` for the URL or key means the run's."""

    model: str
    base_url: str | None = None
    api_key: str | None = None


class JudgeModels(ModelAccessLayer):
    """The judge's Model Access Layer. It uses the run's configuration with the judge's model,
    endpoint and key over it (module docstring)."""

    def __init__(self, base: ModelAccessLayer, judge: JudgeEndpoint) -> None:
        # Set before ``super().__init__``: the adapter it builds resolves through ``self``.
        self.base = base
        self.judge = judge
        super().__init__(base.settings)
        self._ciphertext = self.keybox.encrypt(judge.api_key) if judge.api_key else None
        self.bind(base.db)

    async def resolve(self, purpose: str) -> EffectiveConfig:
        config = await self.base.resolve(purpose)
        return replace(
            config,
            model=self.judge.model,
            base_url=self.judge.base_url or config.base_url,
            api_key_ciphertext=self._ciphertext or config.api_key_ciphertext,
        )


__all__ = ["JudgeEndpoint", "JudgeModels"]
