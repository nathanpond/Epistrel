"""What a configuration's fingerprint needs from the Orchestrator (FR-PROF-16; #109).

Core computes the fingerprint (``core.profiles.fingerprint``) but never reads the model
configuration or the prompt templates: ``runtime_facts`` resolves both here and hands them over
as ``RuntimeFacts``.

- **Models**: for every delivered purpose, the model it resolves to under the deployment's model
  configuration (its own row's ``model``, else the ``default`` row's; ``/admin/model-config``,
  #52), read in the caller's transaction, so a turn's fingerprint comes from the snapshot its
  prompt was read in. No adapter reports a model version in M3, so ``reported_version`` is
  ``null``. Endpoints and keys never enter a fingerprint. These are the deployment's models: a
  configuration that routes a purpose (``models.purposes``) names its own model in the
  fingerprint (``core.profiles.fingerprint.routed_models``, #325), as its calls use it.
- **Templates** (``TEMPLATE_HASHES``): the SHA-256 of each delivered purpose's template file as
  it ships, followed by every template it includes (``template_source``), by template name,
  computed once at import. Editing a template changes every
  fingerprint, as FR-PROF-16's "prompt-template versions" require.
"""

from __future__ import annotations

import hashlib
import re
import types
from collections.abc import Mapping
from importlib import resources

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.profiles import certification
from epistrel.core.profiles.fingerprint import ModelIdentity, RuntimeFacts
from epistrel.orchestrator.models import reported_version
from epistrel.orchestrator.models.config import ConfigRow, parent_of, read_rows
from epistrel.orchestrator.models.registry import DEFAULT_MODEL_KEY, STORY_PURPOSES

TEMPLATE_PACKAGE = "epistrel.orchestrator.models"
TEMPLATE_SUFFIX = ".j2"


INCLUDE = re.compile(rb"""{%-?\s*include\s+["']([^"']+)["']""")
"""A Jinja ``{% include "name.j2" %}`` tag (its template's name)."""


def template_source(name: str) -> bytes:
    """``name``'s bytes followed by those of every template it includes, depth first, each
    once (#826: the character render's knowledge sections live in shared partials, and editing
    one must move the fingerprint as editing the render's own file does)."""
    folder = resources.files(TEMPLATE_PACKAGE).joinpath("templates")
    seen: list[str] = []

    def visit(current: str) -> bytes:
        if current in seen:
            return b""
        seen.append(current)
        source = folder.joinpath(current).read_bytes()
        included = [visit(found.decode()) for found in INCLUDE.findall(source)]
        return source + b"".join(included)

    return visit(name)


def _template_hashes() -> dict[str, str]:
    found: dict[str, str] = {}
    for purpose in STORY_PURPOSES.values():
        for template in (purpose.template, *purpose.variants.values()):
            source = template_source(template)
            found[template.removesuffix(TEMPLATE_SUFFIX)] = hashlib.sha256(source).hexdigest()
    return dict(sorted(found.items()))


TEMPLATE_HASHES: Mapping[str, str] = types.MappingProxyType(_template_hashes())
"""Each story purpose's prompt template's SHA-256, by template name (``STORY_PURPOSES``: a
deployment-level purpose such as ``rating_suggestion`` is no part of a story's runtime)."""


def identities(
    rows: Mapping[str, ConfigRow], versions: Mapping[str, str] | None = None
) -> dict[str, ModelIdentity]:
    """The model each story purpose resolves to under ``rows`` (``model_config``): its own
    row's, else its parent purpose's (``config.parent_of``: ``lite_summary`` → consolidation's,
    ``regeneration`` → generation's, #151), else the ``default`` row's. A purpose that runs
    outside stories (``rating_suggestion``, #121) is not here."""
    default = rows.get(DEFAULT_MODEL_KEY)
    found: dict[str, ModelIdentity] = {}
    for name in sorted(STORY_PURPOSES):
        model = None
        current: str | None = name
        while model is None and current is not None:
            own = rows.get(current)
            model = own.model if own is not None and own.model is not None else None
            current = parent_of(current)
        if model is None and default is not None:
            model = default.model
        found[name] = ModelIdentity(
            id=model, reported_version=(versions or {}).get(model) if model else None
        )
    return found


async def runtime_facts(conn: AsyncConnection) -> RuntimeFacts:
    """The model each delivered purpose resolves to, read in ``conn``'s transaction, the
    template hashes, and the certified fingerprints (#233: one indexed query)."""
    versions = await reported_version.latest(conn)
    return RuntimeFacts(
        models=identities(await read_rows(conn), versions),
        templates=TEMPLATE_HASHES,
        quality=await certification.certified(conn),
        versions=versions,
    )


__all__ = ["TEMPLATE_HASHES", "identities", "runtime_facts", "template_source"]
