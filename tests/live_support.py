"""Shared setup for ``live_model`` tests (E-06, P-08; #54): a Model Access Layer on the
``openai`` adapter pointed at a real endpoint named by ``EPISTREL_LIVE_BASE_URL``,
``EPISTREL_LIVE_MODEL`` and ``EPISTREL_LIVE_API_KEY`` (default ``lm-studio``), and (C-05, #138)
an embedding service on the ``/embeddings`` endpoint ``EPISTREL_LIVE_EMBEDDING_BASE_URL`` and
``EPISTREL_LIVE_EMBEDDING_MODEL`` name (``EPISTREL_LIVE_EMBEDDING_API_KEY``, default the chat
key).

The variables are read when this module is imported, because the suite's autouse fixture strips
every ``EPISTREL_*`` variable from each test's environment. These tests run only when ``-m``
names ``live_model`` (``tests/conftest.py``); a selected test with no endpoint fails rather than
skipping, so a live run can never pass by measuring nothing.
"""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from epistrel.orchestrator.embeddings.adapter import OpenAICompatibleEmbeddingAdapter
from epistrel.orchestrator.embeddings.service import EmbeddingService
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.config import EffectiveConfig, KeyBox
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.tools.model_matrix import Target
from tests.auth_support import auth_settings
from tests.conftest import DUMMY_DSN

LIVE_BASE_URL = os.environ.get("EPISTREL_LIVE_BASE_URL")
LIVE_MODEL = os.environ.get("EPISTREL_LIVE_MODEL")
LIVE_API_KEY = os.environ.get("EPISTREL_LIVE_API_KEY") or "lm-studio"


def live_target() -> Target:
    if not LIVE_BASE_URL or not LIVE_MODEL:
        pytest.fail(
            "live_model tests need EPISTREL_LIVE_BASE_URL and EPISTREL_LIVE_MODEL "
            "(and EPISTREL_LIVE_API_KEY unless the endpoint ignores it)",
            pytrace=False,
        )
    return Target(base_url=LIVE_BASE_URL.rstrip("/"), model=LIVE_MODEL, api_key=LIVE_API_KEY)


def live_layer() -> ModelAccessLayer:
    """A database-free layer whose every purpose uses the live endpoint."""
    target = live_target()
    keybox = KeyBox(SecretStr(Fernet.generate_key().decode()))
    ciphertext = keybox.encrypt(target.api_key)

    async def resolve(purpose: str) -> EffectiveConfig:
        return EffectiveConfig(
            purpose=purpose,
            base_url=target.base_url,
            model=target.model,
            api_key_ciphertext=ciphertext,
            params=ModelParams(),
            revision=0,
        )

    settings = auth_settings(DUMMY_DSN, model_adapter="openai")
    layer = ModelAccessLayer(settings, adapter=OpenAICompatibleAdapter(resolve, keybox))
    layer.bind(None)
    return layer


LIVE_EMBEDDING_BASE_URL = os.environ.get("EPISTREL_LIVE_EMBEDDING_BASE_URL")
LIVE_EMBEDDING_MODEL = os.environ.get("EPISTREL_LIVE_EMBEDDING_MODEL")
LIVE_EMBEDDING_API_KEY = os.environ.get("EPISTREL_LIVE_EMBEDDING_API_KEY") or LIVE_API_KEY


def live_embedding() -> tuple[EmbeddingService, EffectiveConfig]:
    """A database-free embedding service on the ``openai`` adapter and the live ``/embeddings``
    endpoint's configuration (C-05's live case, #138)."""
    if not LIVE_EMBEDDING_BASE_URL or not LIVE_EMBEDDING_MODEL:
        pytest.fail(
            "the live embedding test needs EPISTREL_LIVE_EMBEDDING_BASE_URL and "
            "EPISTREL_LIVE_EMBEDDING_MODEL (and EPISTREL_LIVE_EMBEDDING_API_KEY unless the "
            "endpoint ignores it)",
            pytrace=False,
        )
    keybox = KeyBox(SecretStr(Fernet.generate_key().decode()))
    config = EffectiveConfig(
        purpose="embedding",
        base_url=LIVE_EMBEDDING_BASE_URL.rstrip("/"),
        model=LIVE_EMBEDDING_MODEL,
        api_key_ciphertext=keybox.encrypt(LIVE_EMBEDDING_API_KEY),
        params=ModelParams(),
        revision=0,
    )
    settings = auth_settings(DUMMY_DSN, model_adapter="openai")
    service = EmbeddingService(settings, keybox, OpenAICompatibleEmbeddingAdapter(keybox))
    return service, config
