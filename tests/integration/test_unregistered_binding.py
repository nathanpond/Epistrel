"""#682: ``POST /stories`` with a provider binding naming an unregistered provider is 404
``not_found`` (``ProviderNotFound``), the response the OpenAPI document now declares
(``tests/test_openapi_declared.py``)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import provider_support as ps
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.story_support import WRITE, story_body

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_an_unregistered_binding_is_404(stub_settings: Settings, engine: AsyncEngine) -> None:
    async with respond_client(stub_settings, engine) as (client, _epistrel):
        missing = ps.unique_name("missing")
        refused = await client.post(
            "/stories",
            json=story_body(
                "Wren", "Ada", provider_bindings={missing: {"external_ids": {"player": "p-1"}}}
            ),
            headers=WRITE,
        )
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"]["code"] == "not_found"
        assert refused.json()["error"]["details"]["provider"] == missing
