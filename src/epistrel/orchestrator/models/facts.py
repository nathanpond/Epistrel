"""What a configuration's fingerprint needs from the Orchestrator (FR-PROF-16; #109).

Core computes the fingerprint (``core.profiles.fingerprint``) but never reads the model
configuration or the prompt templates: ``runtime_facts`` resolves both here and hands them over
as ``RuntimeFacts``.

- **Models**: for every delivered purpose, the model it resolves to under the deployment's model
  configuration (its own row's ``model``, else the ``default`` row's; ``/admin/model-config``,
  #52), read in the caller's transaction, so a turn's fingerprint comes from the snapshot its
  prompt was read in. No adapter reports a model version in M3, so ``reported_version`` is
  ``null``. Endpoints and keys never enter a fingerprint.
- **Templates** (``TEMPLATE_HASHES``): the SHA-256 of each delivered purpose's template file as
  it ships, by template name, computed once at import. Editing a template changes every
  fingerprint, as FR-PROF-16's "prompt-template versions" require.
"""

from __future__ import annotations

import hashlib
import types
from collections.abc import Mapping
from importlib import resources

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.profiles.fingerprint import ModelIdentity, RuntimeFacts
from epistrel.orchestrator.models.config import ConfigRow, read_rows
from epistrel.orchestrator.models.registry import DEFAULT_MODEL_KEY, STORY_PURPOSES

TEMPLATE_PACKAGE = "epistrel.orchestrator.models"
TEMPLATE_SUFFIX = ".j2"


def _template_hashes() -> dict[str, str]:
    folder = resources.files(TEMPLATE_PACKAGE).joinpath("templates")
    found: dict[str, str] = {}
    for purpose in STORY_PURPOSES.values():
        source = folder.joinpath(purpose.template).read_bytes()
        found[purpose.template.removesuffix(TEMPLATE_SUFFIX)] = hashlib.sha256(source).hexdigest()
    return dict(sorted(found.items()))


TEMPLATE_HASHES: Mapping[str, str] = types.MappingProxyType(_template_hashes())
"""Each story purpose's prompt template's SHA-256, by template name (``STORY_PURPOSES``: a
deployment-level purpose such as ``rating_suggestion`` is no part of a story's runtime)."""


def identities(rows: Mapping[str, ConfigRow]) -> dict[str, ModelIdentity]:
    """The model each story purpose resolves to under ``rows`` (``model_config``): its own
    row's, else the ``default`` row's. Story purposes default to ``default``; a purpose that
    defaults to another's row (``rating_suggestion``, #121) runs outside stories and is not
    here."""
    default = rows.get(DEFAULT_MODEL_KEY)
    found: dict[str, ModelIdentity] = {}
    for name in sorted(STORY_PURPOSES):
        own = rows.get(name)
        model = own.model if own is not None and own.model is not None else None
        if model is None and default is not None:
            model = default.model
        found[name] = ModelIdentity(id=model)
    return found


async def runtime_facts(conn: AsyncConnection) -> RuntimeFacts:
    """The model each delivered purpose resolves to, read in ``conn``'s transaction, and the
    template hashes."""
    return RuntimeFacts(models=identities(await read_rows(conn)), templates=TEMPLATE_HASHES)


__all__ = ["TEMPLATE_HASHES", "identities", "runtime_facts"]
