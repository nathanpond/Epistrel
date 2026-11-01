"""External-data providers (FR-EXT-1,5,6; NFR-PORT-2; #204): the port, the two adapters, the
registry behind ``/admin/external-providers`` and each story's bindings.

- ``port``: ``ExternalProvider``, ``ReadResult``, ``WriteOutcome``, ``Binding``, ``Effect``
  and ``ProviderUnavailable`` (the adapter error the unavailability policy handles, #206).
- ``manifest``: the registration and manifest models and their validation.
- ``http_manifest``: ``HttpManifestProvider`` (template substitution, the path subset, the
  https and allowed-hosts rules, secrets from the environment).
- ``in_process``: ``InProcessProvider`` over ``Engine.register_in_process_provider``'s
  callables.
- ``registry``: registration writes (configuration changes), reads and ``provider_for``.
- ``bindings``: ``bind_story``, ``story_bindings`` and the turn-start guard ``require_bound``.
"""

from epistrel.orchestrator.providers.bindings import (
    BindingWrite,
    bind_story,
    require_bound,
    story_bindings,
)
from epistrel.orchestrator.providers.http_manifest import HttpManifestProvider
from epistrel.orchestrator.providers.in_process import InProcessProvider, InProcessRegistry
from epistrel.orchestrator.providers.manifest import ProviderRegistration, ProviderSpec
from epistrel.orchestrator.providers.port import (
    Binding,
    Effect,
    ExternalProvider,
    ProviderUnavailable,
    ReadResult,
    WriteOutcome,
)
from epistrel.orchestrator.providers.registry import ProviderWrite, provider_for

__all__ = [
    "Binding",
    "BindingWrite",
    "Effect",
    "ExternalProvider",
    "HttpManifestProvider",
    "InProcessProvider",
    "InProcessRegistry",
    "ProviderRegistration",
    "ProviderSpec",
    "ProviderUnavailable",
    "ProviderWrite",
    "ReadResult",
    "WriteOutcome",
    "bind_story",
    "provider_for",
    "require_bound",
    "story_bindings",
]
