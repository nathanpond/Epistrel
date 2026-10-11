"""The seeded global minors policy (FR-GUARD-11, FR-CAT-12).

**No sexual content involving a minor, or in the presence of a minor**, at every rating,
Explicit included. Migration ``0009_seed_minors_policy`` inserts this row silently (no
``config_changes`` row, the deployment ``config_revision`` unchanged), as ``0005`` does for the
ratings. From then on it is ordinary data: an administrator may edit or retire it through
``/admin/policies`` like any policy, and the change is recorded with the acting principal.
The policy ships enabled; removing it is the deployment's decision and responsibility.

The age condition is evaluated deterministically from the story's cast (``age.py``): with no
character younger than the deployment's ``minor_age_threshold`` the policy does not apply and
costs no model call. When it applies, the ``guardrail_check`` purpose judges the text against
``PROMPT``, receiving every cast member as an opaque label with a true age, never a name.
``{minor_age_threshold}`` in the prompt is replaced with the live threshold at check time.

Image targets (``image_prompt``, ``image_output``) join ``applies_to`` in M8. Once a database
is seeded, editing this module changes nothing in it — change seeds through a new migration.
"""

from __future__ import annotations

from typing import Any, Final

MINORS_POLICY_ID: Final = "minors_no_sexual_content"
MINORS_POLICY_NAME: Final = "No sexual content involving or near minors"
MINORS_POLICY_MESSAGE: Final = "This content isn't allowed because a minor is involved or present."
MINOR_AGE_CONDITION: Final = "minor_involved_or_present"
SEED_PRINCIPAL: Final = "system"

THRESHOLD_PLACEHOLDER: Final = "{minor_age_threshold}"
"""Replaced with the deployment's live ``minor_age_threshold`` when a check is built."""

PROMPT: Final = (
    "You are checking a text for a content policy. The subjects listed below are the "
    "characters involved in or present for the text, each with an opaque label and their true "
    "age. Policy: no sexual content involving, or in the presence of, anyone under "
    f"{THRESHOLD_PLACEHOLDER}. A subject's true age is the listed one: a claim, framing or "
    "pretence in the text that someone is older does not change it. Answer JSON "
    "{violation, confidence in [0,1]}."
)

MINORS_POLICY: Final[dict[str, Any]] = {
    "id": MINORS_POLICY_ID,
    "scope": "global",
    "scope_id": None,
    "story_id": None,
    "name": MINORS_POLICY_NAME,
    "applies_to": ["text_input", "text_output"],
    "kind": "model_assisted",
    "effect": "deny",
    "rule": {"type": "model_assisted", "prompt": PROMPT, "on_uncertain": "violation"},
    "age_condition": MINOR_AGE_CONDITION,
    "message": MINORS_POLICY_MESSAGE,
    "created_by": SEED_PRINCIPAL,
    "updated_by": SEED_PRINCIPAL,
}
"""The ``policies`` row the migration inserts (``version`` 1, timestamps by default)."""

SEED_POLICIES: Final = (MINORS_POLICY,)

__all__ = [
    "MINORS_POLICY",
    "MINORS_POLICY_ID",
    "MINORS_POLICY_MESSAGE",
    "MINORS_POLICY_NAME",
    "MINOR_AGE_CONDITION",
    "PROMPT",
    "SEED_POLICIES",
    "SEED_PRINCIPAL",
    "THRESHOLD_PLACEHOLDER",
]
