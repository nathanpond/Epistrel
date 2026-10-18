"""The embedding side of the Model Access Layer (FR-EMBED-1, FR-MODEL-12; #138).

``EmbeddingService`` (``ModelAccessLayer.embeddings``) is the one way Epistrel embeds text:

- ``embed(texts)`` resolves the ``embedding`` row of ``model_config`` (no fallback to the
  ``default`` row; an unset model is 503 ``model_unavailable`` ``not_configured`` under the
  ``openai`` adapter), cuts every text to the port's input floor (``len // 4`` tokens,
  ``EmbeddingFloor.min_input_tokens``), and sends batches of at most ``embedding_batch_max``
  texts (a deployment setting, default 64; never more than the floor's batch). Each batch is
  one attempt, bounded by the row's ``params.timeout`` (default 60 s) and by ``deadline`` (a
  turn's or a job lease's remaining seconds) when given, whichever is sooner. There are no
  adapter retries. Every failure leaves as a ``ModelError`` with ``details.purpose =
  embedding``; every batch is counted in ``epistrel_model_calls_total{purpose="embedding"}`` and
  the operation's ``model_calls`` (``metrics.record``), separately from any ``TurnBudget``.
  All batches of one call must agree on a dimension.
- ``identity(config)`` names the vector space a configuration embeds into (``version``, the
  string every vector is stamped with) without a call.
- ``probe(config)`` is the protocol probe: ``GET /models`` plus one embedding, answering the
  endpoint's dimension (``/admin/model-config/embedding`` probes a new model before the write;
  the model matrix reports it).
- ``query_embedder()`` is the ``core.retrieval.Embedder`` a search is given.

The adapter is ``EPISTREL_EMBEDDING_ADAPTER``'s (``openai`` or ``stub``; unset:
``EPISTREL_MODEL_ADAPTER``'s). Nothing here logs a text (invariant 3).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.config_changes import read_section
from epistrel.core.errors import InvalidRequest
from epistrel.core.settings_schema import EmbeddingSettings
from epistrel.orchestrator.embeddings.adapter import (
    EMBEDDING,
    PROBE_TEXT,
    EmbeddingAdapter,
    EmbeddingIdentity,
    EmbeddingProbe,
    EmbeddingResult,
    OpenAICompatibleEmbeddingAdapter,
    StubEmbeddingAdapter,
)
from epistrel.orchestrator.models.adapter import DEFAULT_TIMEOUT_S, PROBE_TIMEOUT_S, ModelParams
from epistrel.orchestrator.models.config import ConfigCache, EffectiveConfig, KeyBox
from epistrel.orchestrator.models.errors import ModelError, ModelUnavailable, map_provider_error
from epistrel.orchestrator.models.metrics import ModelCall, call_started, outcome_of, record
from epistrel.orchestrator.models.registry import EMBEDDING as PORT

CHARS_PER_TOKEN = 4
MAX_INPUT_CHARS = PORT.floor.min_input_tokens * CHARS_PER_TOKEN
"""A text longer than the floor's token estimate is cut to it before it is sent."""


@dataclass(frozen=True)
class Embedded:
    """What one ``embed`` call answered: a vector per text, their dimension, the vector space
    (``identity``) and the model the endpoint reported."""

    vectors: tuple[tuple[float, ...], ...]
    dimension: int
    identity: EmbeddingIdentity
    reported_model: str
    calls: int


class EmbeddingUnreachable(InvalidRequest):
    """A new embedding model did not answer its protocol probe, so it was not configured
    (``PUT /admin/model-config/embedding``). ``details``: ``purpose`` and ``reason`` (the
    ``model_unavailable`` reason: ``connection``, ``auth``, ``not_configured``, …)."""

    code = "embedding_unreachable"

    def __init__(self, reason: str) -> None:
        super().__init__(
            "the embedding endpoint did not answer the probe; the model was not changed",
            details={"field": "body.model", "purpose": EMBEDDING, "reason": reason},
        )


def make_adapter(settings: Settings, keybox: KeyBox) -> EmbeddingAdapter:
    if settings.embedding_adapter_kind == "stub":
        return StubEmbeddingAdapter(dimension=settings.stub_embedding_dimension)
    return OpenAICompatibleEmbeddingAdapter(keybox)


def _unconfigured() -> EffectiveConfig:
    return EffectiveConfig(
        purpose=EMBEDDING,
        base_url=None,
        model=None,
        api_key_ciphertext=None,
        params=ModelParams(),
        revision=0,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


class EmbeddingService:
    """The embedding port's adapter, its configuration and its accounting."""

    def __init__(
        self, settings: Settings, keybox: KeyBox, adapter: EmbeddingAdapter | None = None
    ) -> None:
        self.settings = settings
        self.keybox = keybox
        self.cache = ConfigCache()
        self.adapter: EmbeddingAdapter = adapter or make_adapter(settings, keybox)
        self._db: AsyncEngine | None = None

    def bind(self, db: AsyncEngine | None) -> None:
        self._db = db
        self.cache.clear()

    @property
    def stub(self) -> StubEmbeddingAdapter:
        """The stub adapter (``EPISTREL_EMBEDDING_ADAPTER=stub`` only)."""
        if not isinstance(self.adapter, StubEmbeddingAdapter):
            raise RuntimeError("the embedding adapter is not the stub")
        return self.adapter

    async def config(self) -> EffectiveConfig:
        """The ``embedding`` row as of the current ``config_revision`` (an unset row without a
        database)."""
        if self._db is None:
            return _unconfigured()
        return await self.cache.effective(self._db, EMBEDDING)

    async def identity(self, config: EffectiveConfig | None = None) -> EmbeddingIdentity:
        return self.adapter.identity(config or await self.config())

    async def _batch_max(self) -> int:
        if self._db is None:
            return PORT.floor.max_batch
        async with self._db.connect() as conn:
            settings = await read_section(conn, EmbeddingSettings)
        return max(1, min(settings.embedding_batch_max, PORT.floor.max_batch))

    async def _send(
        self, texts: Sequence[str], config: EffectiveConfig, *, limit: float, attempt: int
    ) -> EmbeddingResult:
        model = config.model or self.adapter.name
        call_started()
        started = time.perf_counter()
        bound = asyncio.timeout(limit)
        try:
            async with bound:
                result = await self.adapter.embed(texts, config, timeout_s=limit)
        except TimeoutError:
            if not bound.expired():
                raise
            error: ModelError = ModelUnavailable(EMBEDDING, "timeout")
            self._failed(model, attempt, started, error)
            raise error from None
        except asyncio.CancelledError as exc:
            self._failed(model, attempt, started, exc)
            raise
        except Exception as exc:
            mapped = map_provider_error(EMBEDDING, exc)
            if mapped is None:
                raise
            if mapped.details.get("reason") not in ("not_configured", "no_api_key"):
                self._failed(model, attempt, started, mapped)
            # ``from None``: a provider's message can echo the input (invariant 3).
            raise mapped from None
        record(
            ModelCall(
                purpose=EMBEDDING,
                model=model,
                attempt=attempt,
                outcome="ok",
                latency_ms=_elapsed_ms(started),
                tokens_in=result.tokens_in or 0,
                tokens_out=0,
            )
        )
        return result

    @staticmethod
    def _failed(model: str, attempt: int, started: float, error: BaseException) -> None:
        record(
            ModelCall(
                purpose=EMBEDDING,
                model=model or "unknown",
                attempt=attempt,
                outcome=outcome_of(error),
                latency_ms=_elapsed_ms(started),
                tokens_in=0,
                tokens_out=0,
            )
        )

    async def embed(
        self,
        texts: Sequence[str],
        *,
        config: EffectiveConfig | None = None,
        deadline: float | None = None,
    ) -> Embedded:
        """Embed ``texts`` (see the module docstring); ``deadline`` is the seconds left to the
        caller (a turn's deadline, a job's lease)."""
        config = config or await self.config()
        identity = self.adapter.identity(config)
        if not texts:
            return Embedded((), 0, identity, identity.model, 0)
        batch = await self._batch_max()
        timeout = config.params.timeout or DEFAULT_TIMEOUT_S
        started = time.monotonic()
        vectors: list[tuple[float, ...]] = []
        dimension: int | None = None
        reported = identity.model
        calls = 0
        cut = [text[:MAX_INPUT_CHARS] for text in texts]
        for start in range(0, len(cut), batch):
            limit = timeout
            if deadline is not None:
                left = deadline - (time.monotonic() - started)
                if left <= 0:
                    raise ModelUnavailable(EMBEDDING, "deadline")
                limit = min(limit, left)
            calls += 1
            result = await self._send(cut[start : start + batch], config, limit=limit, attempt=1)
            if dimension is None:
                dimension = result.dimension
            elif result.dimension != dimension:
                raise ModelUnavailable(EMBEDDING, "garbage", "the batches differ in dimension")
            vectors.extend(result.vectors)
            reported = result.model
        return Embedded(tuple(vectors), int(dimension or 0), identity, reported, calls)

    async def probe(self, config: EffectiveConfig | None = None) -> EmbeddingProbe:
        """``GET /models`` and one embedding against ``config`` (the current row by default),
        bounded by ``PROBE_TIMEOUT_S``; every failure leaves as a ``ModelError``."""
        config = config or await self.config()
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S):
                listed = await self.adapter.list_models(config, timeout_s=PROBE_TIMEOUT_S)
        except TimeoutError:
            raise ModelUnavailable(EMBEDDING, "timeout") from None
        except Exception as exc:
            mapped = map_provider_error(EMBEDDING, exc)
            if mapped is None:
                raise
            raise mapped from None
        embedded = await self.embed([PROBE_TEXT], config=config, deadline=PROBE_TIMEOUT_S)
        return EmbeddingProbe(
            protocol_ok=True,
            models=listed,
            model=embedded.reported_model,
            dimension=embedded.dimension,
            version=embedded.identity.version,
        )

    def query_embedder(self) -> Callable[[str], Awaitable[Sequence[float]]]:
        """A ``core.retrieval.Embedder``: one query text to its vector."""

        async def embed_query(query: str) -> Sequence[float]:
            return (await self.embed([query])).vectors[0]

        return embed_query

    def batch_embedder(self) -> Callable[[Sequence[str]], Awaitable[Sequence[Sequence[float]]]]:
        """A ``core.retrieval.pipeline.BatchEmbedder``: several texts to their vectors in one
        call (a retrieval's rehydration, #141)."""

        async def embed_texts(texts: Sequence[str]) -> Sequence[Sequence[float]]:
            if not texts:
                return []
            return (await self.embed(list(texts))).vectors

        return embed_texts

    async def aclose(self) -> None:
        await self.adapter.aclose()


__all__ = [
    "MAX_INPUT_CHARS",
    "Embedded",
    "EmbeddingService",
    "EmbeddingUnreachable",
    "make_adapter",
]
