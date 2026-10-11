"""The Model Access Layer (docs/02 §5): the purpose registry, prompt templates, per-purpose
configuration, and the two adapters (``openai`` SDK and the deterministic stub).

``epistrel.core`` never imports this package (import-linter contract
``core-does-not-import-orchestrator-or-api``), so swapping adapters changes nothing in Core.
"""

from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import (
    Message,
    ModelAdapter,
    ModelError,
    ModelParams,
    ModelResult,
    ModelUnavailable,
)
from epistrel.orchestrator.models.errors import (
    BudgetExhausted,
    EmptyCompletion,
    ResponseFormatRejected,
)
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    RESERVED,
    ContentCategory,
    GuardrailVerdict,
    Purpose,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.models.stub import StubAdapter

__all__ = [
    "DELIVERED",
    "RESERVED",
    "BudgetExhausted",
    "ContentCategory",
    "EmptyCompletion",
    "GuardrailVerdict",
    "Message",
    "ModelAccessLayer",
    "ModelAdapter",
    "ModelError",
    "ModelParams",
    "ModelResult",
    "ModelUnavailable",
    "Purpose",
    "ResponseFormatRejected",
    "StubAdapter",
    "Uncertain",
]
