"""Every table the engine owns, declared against one ``metadata`` (imported by ``core.schema``).

The message-feedback tables (#112) are registered here by importing their module, but their
names are not re-exported: a module that reads or writes feedback imports
``epistrel.core.tables.feedback`` by name, so the guard over ``src/`` that lists every module
naming them (``tests/integration/test_feedback.py``) sees each one (FR-FB-2).
"""

from epistrel.core.tables import feedback as _feedback  # noqa: F401 - registers the tables
from epistrel.core.tables.audit_log import audit_log
from epistrel.core.tables.base import NAMING_CONVENTION, metadata
from epistrel.core.tables.beliefs import (
    belief_state,
    belief_transitions,
    beliefs,
    nested_belief_state,
)
from epistrel.core.tables.catalog import (
    character_catalog,
    character_versions,
    scenario_catalog,
    scenario_versions,
)
from epistrel.core.tables.characters import character_snapshots, characters
from epistrel.core.tables.client_events import client_events
from epistrel.core.tables.commitments import promissory_ledger, thunks
from epistrel.core.tables.deployment import config_changes, deployment_state
from epistrel.core.tables.detection import extraction_shadow
from epistrel.core.tables.divergences import divergences
from epistrel.core.tables.erasure import erasure_ledger
from epistrel.core.tables.escalations import (
    escalations,
    gate_threshold_history,
    gate_thresholds,
)
from epistrel.core.tables.events import events
from epistrel.core.tables.exposures import exposures
from epistrel.core.tables.facts import facts
from epistrel.core.tables.genesis import genesis_deferred
from epistrel.core.tables.hot_set import overflow_episodes
from epistrel.core.tables.idempotency import idempotency_keys
from epistrel.core.tables.jobs import jobs
from epistrel.core.tables.media import media_assets, subject_references
from epistrel.core.tables.memories import memories
from epistrel.core.tables.model_config import model_config
from epistrel.core.tables.offboarding import offboarding_jobs
from epistrel.core.tables.operations import operations
from epistrel.core.tables.ownership import entity_relations, ownership_events
from epistrel.core.tables.personas import personas
from epistrel.core.tables.policies import policies
from epistrel.core.tables.profiles import (
    engine_profile_versions,
    engine_profiles,
    story_profile_overrides,
)
from epistrel.core.tables.projections import (
    lite_summary_segments,
    placement,
    relationships,
    scene_state,
    story_heads,
    story_texts,
    transcript_messages,
)
from epistrel.core.tables.propositions import (
    closed_sets,
    entities,
    entity_aliases,
    predicate_proposals,
    predicates,
    propositions,
)
from epistrel.core.tables.providers import external_providers, story_provider_bindings
from epistrel.core.tables.rating_checks import guardrail_check_results, rating_check_jobs
from epistrel.core.tables.ratings import content_rating_versions, content_ratings
from epistrel.core.tables.readset import proposition_changes
from epistrel.core.tables.states import (
    external_values,
    state_changes,
    state_definitions,
    state_proposals,
    state_values,
)
from epistrel.core.tables.stories import stories
from epistrel.core.tables.timers import recurring_tasks, timers, worker_heartbeats
from epistrel.core.tables.turn_queue import turn_queue
from epistrel.core.tables.utterances import utterances

__all__ = [
    "NAMING_CONVENTION",
    "audit_log",
    "belief_state",
    "belief_transitions",
    "beliefs",
    "character_catalog",
    "character_snapshots",
    "character_versions",
    "characters",
    "client_events",
    "closed_sets",
    "config_changes",
    "content_rating_versions",
    "content_ratings",
    "deployment_state",
    "divergences",
    "engine_profile_versions",
    "engine_profiles",
    "entities",
    "entity_aliases",
    "entity_relations",
    "erasure_ledger",
    "escalations",
    "events",
    "exposures",
    "external_providers",
    "external_values",
    "extraction_shadow",
    "facts",
    "gate_threshold_history",
    "gate_thresholds",
    "genesis_deferred",
    "guardrail_check_results",
    "idempotency_keys",
    "jobs",
    "lite_summary_segments",
    "media_assets",
    "memories",
    "metadata",
    "model_config",
    "nested_belief_state",
    "offboarding_jobs",
    "operations",
    "overflow_episodes",
    "ownership_events",
    "personas",
    "placement",
    "policies",
    "predicate_proposals",
    "predicates",
    "promissory_ledger",
    "proposition_changes",
    "propositions",
    "rating_check_jobs",
    "recurring_tasks",
    "relationships",
    "scenario_catalog",
    "scenario_versions",
    "scene_state",
    "state_changes",
    "state_definitions",
    "state_proposals",
    "state_values",
    "stories",
    "story_heads",
    "story_profile_overrides",
    "story_provider_bindings",
    "story_texts",
    "subject_references",
    "thunks",
    "timers",
    "transcript_messages",
    "turn_queue",
    "utterances",
    "worker_heartbeats",
]
