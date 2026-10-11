"""Every table the engine owns, declared against one ``metadata`` (imported by ``core.schema``)."""

from epistrel.core.tables.base import NAMING_CONVENTION, metadata
from epistrel.core.tables.events import events
from epistrel.core.tables.operations import operations
from epistrel.core.tables.projections import story_heads, transcript_messages
from epistrel.core.tables.stories import stories

__all__ = [
    "NAMING_CONVENTION",
    "events",
    "metadata",
    "operations",
    "stories",
    "story_heads",
    "transcript_messages",
]
