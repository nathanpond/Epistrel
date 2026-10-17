"""The embedding port's adapter contract and its two implementations (FR-EMBED-1, NFR-PORT-2;
docs/02 §6; #138).

``EmbeddingAdapter.embed(texts, config, timeout_s=…)`` sends one batch of texts and answers one
vector per text, in order. Two implementations ship; ``EPISTREL_EMBEDDING_ADAPTER`` picks one
(unset: ``EPISTREL_MODEL_ADAPTER``'s), and nothing in ``epistrel.core`` knows which:

- ``OpenAICompatibleEmbeddingAdapter``: the ``openai`` SDK's ``embeddings.create`` against any
  OpenAI-compatible ``/embeddings`` endpoint (``encoding_format=float``, the SDK's retries off),
  configured by the ``embedding`` row of ``model_config`` alone (a port never falls back to the
  ``default`` row). Its version is ``<base_url host>/<model>``.
- ``StubEmbeddingAdapter``: deterministic. A text's vector is the SHA-256 of the text (salted
  with the configured model's name, when one is configured, so a model change is a different
  vector space) expanded by iterated hashing to ``dimension`` floats in [-1, 1] and
  L2-normalised (``stub_vector``); the same text gives the same vector in every process
  (NFR-DETERM-1). Its version is ``stub:sha256:<d>`` (``stub:<model>:sha256:<d>`` with a
  configured model) and its dimension ``EPISTREL_STUB_EMBEDDING_DIMENSION`` (default 64). Tests
  drive it: ``dimension`` may be changed, ``fault(kind, n)`` fails the next ``n`` calls with the
  ``openai`` SDK's own exceptions (``timeout``, ``connection``, ``http_500``, ``http_401``) or
  answers ``garbage`` (a short vector list), and ``calls`` records each batch's size (never its
  texts).

Transport failures escape as the SDK's exception types (or ``ProviderGarbage`` for a 200 that is
not an embedding list); ``EmbeddingService`` maps them to ``ModelUnavailable`` with
``details.purpose = embedding``. Logs carry the model, batch size, latency and the key's
fingerprint only, never a text (invariant 3).
"""

from __future__ import annotations

import hashlib
import logging
import math
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx2
import openai
from pydantic import BaseModel, ConfigDict

from epistrel.orchestrator.models.config import EffectiveConfig, KeyBox, key_fingerprint
from epistrel.orchestrator.models.errors import ModelUnavailable, ProviderGarbage

log = logging.getLogger(__name__)

EMBEDDING = "embedding"
"""The port's name: its ``model_config`` row, its metrics label and its jobs' purpose."""
STUB_EMBEDDING_URL = "http://stub.invalid/v1/embeddings"
PROBE_TEXT = "ready"
"""The protocol probe's one text: no story content."""

type EmbeddingFault = Literal["timeout", "connection", "http_500", "http_401", "garbage"]


@dataclass(frozen=True)
class EmbeddingIdentity:
    """The vector space a configuration embeds into: ``version`` (what every vector is stamped
    with) and the configured ``model``. ``host_bound`` versions (``<host>/<model>``) name the
    space by their model: a ``base_url`` change alone moves no vector space (#138)."""

    version: str
    model: str
    host_bound: bool = False

    def matches(self, pinned_version: str) -> bool:
        """Whether a deployment pinned at ``pinned_version`` is this vector space."""
        if self.host_bound:
            return pinned_version.partition("/")[2] == self.model
        return pinned_version == self.version


class EmbeddingResult(BaseModel):
    """One batch's vectors, in the order of its texts."""

    model_config = ConfigDict(frozen=True)

    vectors: tuple[tuple[float, ...], ...]
    model: str
    """The model the endpoint reported (may differ from the configured one)."""
    dimension: int
    latency_ms: int
    tokens_in: int | None = None


class EmbeddingProbe(BaseModel):
    """A protocol probe's answer: the endpoint lists models and embeds one text."""

    model_config = ConfigDict(frozen=True)

    protocol_ok: bool
    models: tuple[str, ...]
    model: str
    dimension: int
    version: str


def openai_version(config: EffectiveConfig) -> str:
    """``<base_url host>/<model>``: the version an ``openai`` endpoint's vectors carry."""
    host = urlsplit(config.base_url or "").netloc or "unknown"
    return f"{host}/{config.model or ''}"


def stub_vector(text: str, dimension: int, salt: str = "") -> list[float]:
    """The stub's vector for ``text``: SHA-256 of ``salt`` and the text, expanded by iterated
    hashing (``h₀ = sha256(salt NUL text)``, ``hᵢ₊₁ = sha256(hᵢ)``) into ``dimension`` 16-bit
    values mapped to [-1, 1], then L2-normalised."""
    if dimension < 1:
        raise ValueError("a vector has at least one dimension")
    digest = hashlib.sha256(f"{salt}\0{text}".encode()).digest()
    raw = bytearray()
    while len(raw) < 2 * dimension:
        raw += digest
        digest = hashlib.sha256(digest).digest()
    values = [
        (v / 65_535.0) * 2.0 - 1.0 for v in struct.unpack(f">{dimension}H", raw[: 2 * dimension])
    ]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


@runtime_checkable
class EmbeddingAdapter(Protocol):
    """One embedding transport."""

    name: str

    def identity(self, config: EffectiveConfig) -> EmbeddingIdentity:
        """The version and model ``config`` embeds with (no call is made)."""
        ...

    async def embed(
        self, texts: Sequence[str], config: EffectiveConfig, *, timeout_s: float
    ) -> EmbeddingResult: ...

    async def list_models(self, config: EffectiveConfig, *, timeout_s: float) -> tuple[str, ...]:
        """``GET /models`` of the endpoint (the protocol probe's first half)."""
        ...

    async def aclose(self) -> None: ...


# -- the stub ---------------------------------------------------------------------------------


def _fault_error(kind: EmbeddingFault) -> Exception | None:
    request = httpx2.Request("POST", STUB_EMBEDDING_URL)
    if kind == "timeout":
        return openai.APITimeoutError(request=request)
    if kind == "connection":
        return openai.APIConnectionError(request=request)
    if kind == "http_500":
        response = httpx2.Response(500, request=request)
        return openai.InternalServerError("stub fault: http_500", response=response, body=None)
    if kind == "http_401":
        response = httpx2.Response(401, request=request)
        return openai.AuthenticationError("stub fault: http_401", response=response, body=None)
    return None


@dataclass
class StubEmbeddingAdapter:
    """Deterministic vectors and fault injection (see the module docstring)."""

    dimension: int = 64
    name: str = "stub"
    calls: list[int] = field(default_factory=list)
    """Each batch's size, in call order (texts are never kept)."""
    _faults: list[EmbeddingFault] = field(default_factory=list)

    def identity(self, config: EffectiveConfig) -> EmbeddingIdentity:
        model = config.model
        if model:
            return EmbeddingIdentity(f"stub:{model}:sha256:{self.dimension}", model)
        return EmbeddingIdentity(f"stub:sha256:{self.dimension}", "stub")

    def fault(self, kind: EmbeddingFault, n: int = 1) -> None:
        """Fail (or garble) the next ``n`` calls."""
        self._faults.extend([kind] * n)

    def reset(self) -> None:
        self._faults.clear()
        self.calls.clear()

    async def embed(
        self, texts: Sequence[str], config: EffectiveConfig, *, timeout_s: float
    ) -> EmbeddingResult:
        self.calls.append(len(texts))
        if self._faults:
            kind = self._faults.pop(0)
            error = _fault_error(kind)
            if error is not None:
                raise error
            raise ProviderGarbage("embedding: the stub answered garbage")
        salt = config.model or ""
        vectors = tuple(tuple(stub_vector(text, self.dimension, salt)) for text in texts)
        return EmbeddingResult(
            vectors=vectors,
            model=config.model or "stub",
            dimension=self.dimension,
            latency_ms=0,
            tokens_in=sum(max(1, len(t) // 4) for t in texts),
        )

    async def list_models(self, config: EffectiveConfig, *, timeout_s: float) -> tuple[str, ...]:
        if self._faults and self._faults[0] != "garbage":
            error = _fault_error(self._faults.pop(0))
            if error is not None:
                raise error
        return (config.model or "stub",)

    async def aclose(self) -> None:
        return None


# -- the openai SDK adapter -------------------------------------------------------------------


class OpenAICompatibleEmbeddingAdapter:
    """``/embeddings`` over the ``openai`` SDK, configured by the ``embedding`` row."""

    name = "openai"

    def __init__(self, keybox: KeyBox) -> None:
        self._keybox = keybox
        self._clients: dict[tuple[str, str], openai.AsyncOpenAI] = {}

    def identity(self, config: EffectiveConfig) -> EmbeddingIdentity:
        return EmbeddingIdentity(openai_version(config), config.model or "", host_bound=True)

    def _client(self, config: EffectiveConfig) -> tuple[openai.AsyncOpenAI, str]:
        if config.base_url is None or config.model is None:
            raise ModelUnavailable(EMBEDDING, "not_configured")
        if config.api_key_ciphertext is None:
            raise ModelUnavailable(EMBEDDING, "no_api_key")
        secret = self._keybox.decrypt(config.api_key_ciphertext, purpose=EMBEDDING)
        api_key = secret.get_secret_value()
        key = (config.base_url, key_fingerprint(api_key))
        client = self._clients.get(key)
        if client is None:
            client = openai.AsyncOpenAI(base_url=config.base_url, api_key=api_key, max_retries=0)
            self._clients[key] = client
        return client, key[1]

    async def embed(
        self, texts: Sequence[str], config: EffectiveConfig, *, timeout_s: float
    ) -> EmbeddingResult:
        client, fingerprint = self._client(config)
        started = time.perf_counter()
        answer = await client.embeddings.create(
            model=str(config.model), input=list(texts), encoding_format="float", timeout=timeout_s
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        data = getattr(answer, "data", None)
        if not isinstance(data, list) or len(data) != len(texts):
            raise ProviderGarbage("embedding: the response is not one embedding per input")
        ordered = sorted(data, key=lambda item: int(getattr(item, "index", 0) or 0))
        vectors: list[tuple[float, ...]] = []
        for item in ordered:
            values = getattr(item, "embedding", None)
            if not isinstance(values, list) or not values:
                raise ProviderGarbage("embedding: an embedding is not a list of floats")
            try:
                vectors.append(tuple(float(v) for v in values))
            except TypeError, ValueError:
                raise ProviderGarbage("embedding: an embedding is not a list of floats") from None
        dimension = len(vectors[0])
        if any(len(v) != dimension for v in vectors):
            raise ProviderGarbage("embedding: the batch's vectors differ in dimension")
        reported = str(getattr(answer, "model", "") or config.model)
        if reported != config.model:
            log.info("embedding endpoint reported another model reported=%s", reported)
        usage = getattr(answer, "usage", None)
        log.info(
            "embedding call model=%s batch=%d latency_ms=%d key=%s",
            config.model,
            len(texts),
            latency_ms,
            fingerprint,
        )
        return EmbeddingResult(
            vectors=tuple(vectors),
            model=reported,
            dimension=dimension,
            latency_ms=latency_ms,
            tokens_in=getattr(usage, "prompt_tokens", None) if usage is not None else None,
        )

    async def list_models(self, config: EffectiveConfig, *, timeout_s: float) -> tuple[str, ...]:
        client, _ = self._client(config)
        page = await client.models.list(timeout=timeout_s)
        return tuple(sorted(model.id for model in page.data))

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.close()


__all__ = [
    "EMBEDDING",
    "PROBE_TEXT",
    "EmbeddingAdapter",
    "EmbeddingFault",
    "EmbeddingIdentity",
    "EmbeddingProbe",
    "EmbeddingResult",
    "OpenAICompatibleEmbeddingAdapter",
    "StubEmbeddingAdapter",
    "openai_version",
    "stub_vector",
]
