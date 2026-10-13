"""Every table the engine owns, declared against one ``metadata`` (imported by ``core.schema``)."""

from epistrel.core.tables.audit_log import audit_log
from epistrel.core.tables.base import NAMING_CONVENTION, metadata
from epistrel.core.tables.catalog import (
    character_catalog,
    character_versions,
    scenario_catalog,
    scenario_versions,
)
from epistrel.core.tables.characters import characters
from epistrel.core.tables.client_events import client_events
from epistrel.core.tables.deployment import config_changes, deployment_state
from epistrel.core.tables.events import events
from epistrel.core.tables.idempotency import idempotency_keys
from epistrel.core.tables.jobs import jobs
from epistrel.core.tables.media import media_assets, subject_references
from epistrel.core.tables.model_config import model_config
from epistrel.core.tables.operations import operations
from epistrel.core.tables.ownership import entity_relations, ownership_events
from epistrel.core.tables.policies import policies
from epistrel.core.tables.projections import (
    lite_summary_segments,
    story_heads,
    story_texts,
    transcript_messages,
)
from epistrel.core.tables.ratings import content_rating_versions, content_ratings
from epistrel.core.tables.stories import stories
from epistrel.core.tables.timers import recurring_tasks, timers, worker_heartbeats
from epistrel.core.tables.turn_queue import turn_queue

__all__ = [
    "NAMING_CONVENTION",
    "audit_log",
    "character_catalog",
    "character_versions",
    "characters",
    "client_events",
    "config_changes",
    "content_rating_versions",
    "content_ratings",
    "deployment_state",
    "entity_relations",
    "events",
    "idempotency_keys",
    "jobs",
    "lite_summary_segments",
    "media_assets",
    "metadata",
    "model_config",
    "operations",
    "ownership_events",
    "policies",
    "recurring_tasks",
    "scenario_catalog",
    "scenario_versions",
    "stories",
    "story_heads",
    "story_texts",
    "subject_references",
    "timers",
    "transcript_messages",
    "turn_queue",
    "worker_heartbeats",
]
