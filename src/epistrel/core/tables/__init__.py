"""Every table the engine owns, declared against one ``metadata`` (imported by ``core.schema``)."""

from epistrel.core.tables.audit_log import audit_log
from epistrel.core.tables.base import NAMING_CONVENTION, metadata
from epistrel.core.tables.characters import characters
from epistrel.core.tables.deployment import config_changes, deployment_state
from epistrel.core.tables.events import events
from epistrel.core.tables.model_config import model_config
from epistrel.core.tables.operations import operations
from epistrel.core.tables.policies import policies
from epistrel.core.tables.projections import story_heads, transcript_messages
from epistrel.core.tables.ratings import content_rating_versions, content_ratings
from epistrel.core.tables.stories import stories

__all__ = [
    "NAMING_CONVENTION",
    "audit_log",
    "characters",
    "config_changes",
    "content_rating_versions",
    "content_ratings",
    "deployment_state",
    "events",
    "metadata",
    "model_config",
    "operations",
    "policies",
    "stories",
    "story_heads",
    "transcript_messages",
]
