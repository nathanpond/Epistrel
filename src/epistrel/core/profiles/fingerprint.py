"""A configuration's fingerprint, the capabilities it enables, its quality status, and the
guarantees that hold under it (FR-PROF-12,16, FR-VERIFY-15; #109).

**The fingerprint** identifies an effective configuration for quality purposes. It is
``sha256:`` and the SHA-256 hex digest of the canonical JSON (``canonical_json``: keys sorted,
separators ``(",", ":")``, ``ensure_ascii=False``, floats by their shortest ``repr``, decimals
as strings) of::

    {"profile_version": {"profile_id": ..., "version": n},
     "models": {<purpose>: {"id": ..., "reported_version": ...}, ...},
     "quality_relevant_settings": {<dotted path>: value, ..., "prompt_templates": {...}}}

- ``profile_version``: the profile and the version the configuration resolves to.
- ``models``: every model purpose the deployment delivers, with the model id a call for it
  uses (``routed_models``: the configuration's own route, ``models.purposes.<purpose>.model``,
  when it names one, as a profile's model wins over the deployment's, #106 and #325; else the
  one the Orchestrator resolves from ``/admin/model-config``) and the version the provider
  reports (``null`` in M3: no adapter reports one). Endpoints and keys never enter it.
- ``quality_relevant_settings``: every setting the schema tags ``Quality.relevant`` at its
  effective value (a setting the stored document leaves out at its default), and each prompt
  template's SHA-256 under ``prompt_templates`` (FR-PROF-16's "prompt-template versions").
  Settings tagged irrelevant (pinned memories) are not in it, so a story override of one keeps
  the fingerprint; a new profile version is a new fingerprint whatever it changes, since the
  version is an input.

Core never reads the model configuration or the templates: the Orchestrator resolves them and
the Engine passes them in as ``RuntimeFacts``.

**Capabilities** are the modules a configuration turns on, sorted, each by its name or, at a
level that is not simply on, as ``<module>:<level>`` (``verification:entailment``); the pipeline
is named by its level (``lite``, ``structured``; ``capabilities.capability_names``, #129).
**Quality status** is ``{status}``: ``certified``, ``uncertified`` or ``stale``. M3 has no
certification records, so every configuration is ``uncertified``; M7's certification produces
the other two.

**Guarantees** are the FR-VERIFY-15 guarantees a configuration provides, each held where its
capability-map entry holds (``GUARANTEES``, read from ``capabilities.CAPABILITY_MAP``): under
Lite none of them holds.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from epistrel.core.profiles import schema
from epistrel.core.profiles.capabilities import CAPABILITY, CapabilityEntry, capability_names
from epistrel.core.profiles.schema import ProfileSettings, Quality
from epistrel.core.settings_schema import flatten

FINGERPRINT_PREFIX = "sha256:"
FINGERPRINT_PATTERN = r"^sha256:[0-9a-f]{64}$"
TEMPLATES_KEY = "prompt_templates"
"""The key of the prompt templates' hashes inside ``quality_relevant_settings``."""


class QualityStatusValue(StrEnum):
    """FR-PROF-16's quality statuses. ``certified``: a certification matches the fingerprint;
    ``uncertified``: none ever has (every configuration in M3); ``stale``: the matching one was
    invalidated since (by a canary eval below a floor, or by drift)."""

    certified = "certified"
    uncertified = "uncertified"
    stale = "stale"


class QualityStatus(BaseModel):
    """A configuration's quality status (FR-PROF-16). M3 has no certification records, so it is
    always ``uncertified``; M7 adds the certification's id and floors to ``certified``."""

    model_config = ConfigDict(frozen=True)

    status: QualityStatusValue


UNCERTIFIED = QualityStatus(status=QualityStatusValue.uncertified)


class ModelIdentity(BaseModel):
    """One model purpose's model as a fingerprint names it: the configured model id, and the
    version the provider reports (``null`` until an adapter reports one)."""

    model_config = ConfigDict(frozen=True)

    id: str | None
    reported_version: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeFacts:
    """What a fingerprint needs besides the settings, resolved by the Orchestrator: each
    delivered model purpose's model, and each prompt template's SHA-256 by name."""

    models: Mapping[str, ModelIdentity]
    templates: Mapping[str, str]


def routed_models(
    models: Mapping[str, ModelIdentity], settings: ProfileSettings
) -> dict[str, ModelIdentity]:
    """``models`` (the deployment's, as the Orchestrator resolves them) under ``settings``'
    routes: a purpose whose route names a model uses that model (no reported version yet), the
    others keep theirs. A route for a purpose ``models`` does not list adds nothing."""
    found = dict(models)
    for purpose, route in settings.models.purposes.items():
        if purpose in found and route.model is not None:
            found[purpose] = ModelIdentity(id=route.model)
    return found


@dataclass(frozen=True, slots=True)
class Stamp:
    """What every respond response, effective-configuration read and impact report states about
    a configuration (FR-PROF-16): its fingerprint, the capabilities it enables, and its quality
    status; and the model each purpose uses under it (``routed_models``)."""

    fingerprint: str
    capabilities: tuple[str, ...]
    quality_status: QualityStatus
    models: Mapping[str, ModelIdentity]

    def record(self, profile_id: str, version: int) -> dict[str, Any]:
        """What an operation records in ``config_revisions`` beside its revisions (#112 reads
        it): the fingerprint, the profile and version, each purpose's model, the quality status
        and the capabilities."""
        return {
            "fingerprint": self.fingerprint,
            "profile": profile_id,
            "profile_version": version,
            "models": {
                purpose: model.model_dump(mode="json")
                for purpose, model in sorted(self.models.items())
            },
            "quality_status": self.quality_status.model_dump(mode="json"),
            "capabilities": list(self.capabilities),
        }


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON")


def canonical_json(value: Any) -> str:
    """``value`` as one text whatever its key order (see the module docstring)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default
    )


def capabilities(settings: ProfileSettings) -> tuple[str, ...]:
    """The capabilities ``settings`` enables, sorted: each module on, by name or as
    ``<module>:<level>``, and the pipeline by its level (``capabilities.capability_names``)."""
    return capability_names(settings)


RELEVANT_PATHS: frozenset[str] = frozenset(
    path for path, tags in schema.setting_tags().items() if tags.quality is Quality.relevant
)
"""Every setting the schema tags quality-relevant, by dotted path."""


def quality_relevant_settings(
    settings: ProfileSettings, templates: Mapping[str, str]
) -> dict[str, Any]:
    """Every quality-relevant setting by dotted path, at its value under ``settings``, and the
    prompt templates' hashes."""
    values = flatten(ProfileSettings, settings.model_dump(mode="json"))
    relevant: dict[str, Any] = {
        path: value for path, value in values.items() if path in RELEVANT_PATHS
    }
    relevant[TEMPLATES_KEY] = dict(templates)
    return relevant


def fingerprint(
    *, profile_id: str, version: int, settings: ProfileSettings, facts: RuntimeFacts
) -> str:
    """The configuration's fingerprint (see the module docstring)."""
    models = routed_models(facts.models, settings)
    document = {
        "profile_version": {"profile_id": profile_id, "version": version},
        "models": {purpose: model.model_dump(mode="json") for purpose, model in models.items()},
        "quality_relevant_settings": quality_relevant_settings(settings, facts.templates),
    }
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_PREFIX}{digest}"


def stamp(
    *, profile_id: str, version: int, settings: ProfileSettings, facts: RuntimeFacts
) -> Stamp:
    """The configuration's fingerprint, capabilities and quality status."""
    return Stamp(
        fingerprint=fingerprint(
            profile_id=profile_id, version=version, settings=settings, facts=facts
        ),
        capabilities=capabilities(settings),
        quality_status=UNCERTIFIED,
        models=routed_models(facts.models, settings),
    )


@dataclass(frozen=True, slots=True)
class Guarantee:
    """One FR-VERIFY-15 guarantee and the capability-map entry (#129) that provides it."""

    name: str
    entry: str
    """The ``CAPABILITY_MAP`` slug whose modules provide it."""

    @property
    def held_by(self) -> CapabilityEntry:
        return CAPABILITY[self.entry]


GUARANTEES: tuple[Guarantee, ...] = (
    Guarantee("structural knowledge separation", "partitions"),
    Guarantee("audience filtering", "perception_co_location"),
    Guarantee("verification", "verification"),
    Guarantee("commitment", "commitment"),
)
"""The guarantees FR-VERIFY-15 names as not holding under Lite (the strings are its own), each
held where its ``CAPABILITY_MAP`` entry holds: per-character partitions, perception at
``co_location`` or ``full``, verification from declared claims up, and commitment."""


def guarantees(settings: ProfileSettings) -> tuple[str, ...]:
    """The guarantees ``settings`` provides, in ``GUARANTEES`` order: those whose capability-map
    entry holds under it."""
    return tuple(g.name for g in GUARANTEES if g.held_by.holds(settings))


__all__ = [
    "FINGERPRINT_PATTERN",
    "FINGERPRINT_PREFIX",
    "GUARANTEES",
    "RELEVANT_PATHS",
    "TEMPLATES_KEY",
    "UNCERTIFIED",
    "Guarantee",
    "ModelIdentity",
    "QualityStatus",
    "QualityStatusValue",
    "RuntimeFacts",
    "Stamp",
    "canonical_json",
    "capabilities",
    "fingerprint",
    "guarantees",
    "quality_relevant_settings",
    "routed_models",
    "stamp",
]
