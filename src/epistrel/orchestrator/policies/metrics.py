"""Guardrail metrics (#56, #68): counts only, never text (invariant 3, NFR-SEC-4).

``epistrel_guardrail_violations_total{kind,stage}`` counts violated policies: ``kind`` is
``deterministic`` or ``model_assisted``, ``stage`` is ``input`` (#56) or ``output`` (#68).
``epistrel_guardrail_rejections_total{policy_id,stage}`` counts rejected outputs per violated
policy (#68): ``stage`` is ``output`` for a generated draft and ``fallback`` for a safe
fallback line that itself broke a policy.
"""

from __future__ import annotations

from prometheus_client import Counter

GUARDRAIL_VIOLATIONS = Counter(
    "epistrel_guardrail_violations",
    "Policies violated, by policy kind and stage (input or output).",
    ("kind", "stage"),
)

GUARDRAIL_REJECTIONS = Counter(
    "epistrel_guardrail_rejections",
    "Outputs rejected by a guardrail, by violated policy and stage (output or fallback).",
    ("policy_id", "stage"),
)

__all__ = ["GUARDRAIL_REJECTIONS", "GUARDRAIL_VIOLATIONS"]
