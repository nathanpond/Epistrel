"""Every table the engine owns, declared against one ``metadata`` (imported by ``core.schema``)."""

from epistrel.core.tables.base import NAMING_CONVENTION, metadata
from epistrel.core.tables.deployment import config_changes, deployment_state
from epistrel.core.tables.events import events
from epistrel.core.tables.operations import operations
from epistrel.core.tables.projections import story_heads, transcript_messages
from epistrel.core.tables.ratings import content_rating_versions, content_ratings
from epistrel.core.tables.stories import stories

__all__ = [
    "NAMING_CONVENTION",
    "config_changes",
    "content_rating_versions",
    "content_ratings",
    "deployment_state",
    "events",
    "metadata",
    "operations",
    "stories",
    "story_heads",
    "transcript_messages",
]
