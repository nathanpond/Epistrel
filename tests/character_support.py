"""Catalog character documents for the tests (#115): a complete, publishable body in
``CharacterDraft``'s shape, a minimal draft, the visual identity and the phrases planted in each
private field.

The milestone parts (C-15, C-23, R-05, S-SELFPOLICE) take field values from here: their bodies
are checked against the capability lexicon (docs/04 §13), and a character's visual identity names
later milestones' terms (a reference image's ``perceivable`` audience, M5's perception)."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import httpx2 as httpx

from epistrel.core.seeds.ratings import EVERYONE_ID
from tests.auth_support import bearer

PERCEIVABLE = "perceivable"
RESTRICTED = "restricted"
SEEN = PERCEIVABLE
"""A reference image's audience for what anyone who meets the character sees (the parts' name
for it: the word itself is M5's lexicon term)."""
HIDDEN = RESTRICTED
"""A reference image's audience for hidden details."""
OPERATOR = {**bearer(), "X-Epistrel-Principal": "p-operator"}

DISPOSITIONS: dict[str, dict[str, str]] = {
    "threat_response": {"setting": "stands"},
    "dominance": {"setting": "balanced"},
    "honesty": {"setting": "honest", "nuance": "Keeps the secrets she was trusted with."},
    "warmth": {"setting": "cool"},
    "caution": {"setting": "careful"},
}
"""One setting on every disposition axis (a publish needs all five)."""

PRIVATE_FIELDS: dict[str, Any] = {
    "true_age": 41,
    "appearance": "weathered hands that smell of lamp oil",
    "personality": "counts the silver twice before the harbour wakes",
    "likes_dislikes": {
        "likes": ["the hush after a gale"],
        "dislikes": ["men who whistle on the stairs"],
    },
    "dispositions": {
        **DISPOSITIONS,
        "caution": {"setting": "careful", "nuance": "checks the wick three times a night"},
    },
    "values_motivations": "owes nothing to anyone in Port Callan",
    "interaction_style": {
        "speech_style": "answers a question with a question",
        "mannerisms": ["taps the barometer glass twice"],
        "vocabulary": "lighthouse slang from the old keepers",
        "sample_dialogue": ["The light keeps itself, if you let it."],
    },
    "background_flavor": "learned semaphore from a drowned uncle",
    "deflection_style": "turns the talk to the tide tables",
    "safe_fallback_text": "Mara squints at the horizon and says nothing.",
}
"""A complete definition whose every field holds a distinctive phrase: a ``card`` read must show
none of them, a ``full`` read every one."""

PLANTED: tuple[str, ...] = (
    "weathered hands that smell of lamp oil",
    "counts the silver twice before the harbour wakes",
    "the hush after a gale",
    "men who whistle on the stairs",
    "Keeps the secrets she was trusted with.",
    "checks the wick three times a night",
    "owes nothing to anyone in Port Callan",
    "answers a question with a question",
    "taps the barometer glass twice",
    "lighthouse slang from the old keepers",
    "The light keeps itself, if you let it.",
    "learned semaphore from a drowned uncle",
    "turns the talk to the tide tables",
    "Mara squints at the horizon and says nothing.",
)
"""Every phrase ``PRIVATE_FIELDS`` plants (the setting values are vocabulary, not phrases)."""


def definition(name: str = "Mara Venn", **changes: Any) -> dict[str, Any]:
    """A definition holding everything a publish needs, with ``changes`` applied (a ``None``
    value removes a field)."""
    found: dict[str, Any] = {
        "true_age": 41,
        "appearance": f"{name} is tall and weathered.",
        "personality": f"{name} is patient, private and slow to trust.",
        "likes_dislikes": {"likes": ["storms"], "dislikes": ["crowds"]},
        "dispositions": copy.deepcopy(DISPOSITIONS),
        "values_motivations": "Keeping the light burning.",
        "interaction_style": {
            "speech_style": "Short sentences, dry humour.",
            "mannerisms": [],
            "vocabulary": "Plain and nautical.",
            "sample_dialogue": [],
        },
    }
    for key, value in changes.items():
        if value is None:
            found.pop(key, None)
        else:
            found[key] = value
    return found


def complete(
    name: str = "Mara Venn",
    *,
    public_description: str | None = None,
    definition_changes: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    images: Mapping[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A ``POST /catalog/characters`` body that publishes as it is: the public card, a complete
    definition, the Everyone rating; ``metadata`` and ``images`` add to their groups, and
    ``fields`` are further top-level keys (``owner``, ``visual_identity``)."""
    card: dict[str, Any] = {
        "name": name,
        "public_description": public_description or f"{name} keeps the light on Gull Rock.",
    }
    if images is not None:
        card["images"] = dict(images)
    return {
        "public_card": card,
        "definition": definition(name, **dict(definition_changes or {})),
        "metadata": {"content_rating_id": EVERYONE_ID, **dict(metadata or {})},
        **fields,
    }


def named(name: str, **fields: Any) -> dict[str, Any]:
    """A minimal draft: only a name (a draft needs nothing more; a publish does)."""
    return {"public_card": {"name": name}, **fields}


def visual_identity(*references: tuple[str, str | None], **fields: Any) -> dict[str, Any]:
    """A visual identity with everything a publish needs, and one reference image per
    ``(media id, audience)`` (``None``: the audience left out)."""
    found: dict[str, Any] = {
        "preferred_image_model": "sdxl-lightning",
        "base_appearance_prompt": "a tall woman in an oilskin coat, grey braid",
        "negative_prompt": "text, watermark",
        "seed": 4_294_967_295,
        "reference_images": [
            {"media_id": media_id}
            if audience is None
            else {"media_id": media_id, "audience": audience}
            for media_id, audience in references
        ],
        "adapter_params": {"lora": {"name": "mara-v1", "weight": 0.8}, "steps": 30},
        "restricted_details_prompt": "the burn scar under her left sleeve",
    }
    found.update(fields)
    return found


async def require_primary_image(client: httpx.AsyncClient, on: bool) -> None:
    """Turn the deployment's ``require_primary_image`` on or off (``PUT /admin/settings``)."""
    response = await client.put(
        "/admin/settings", json={"require_primary_image": on}, headers=OPERATOR
    )
    assert response.status_code == 200, response.text
    assert response.json()["require_primary_image"] is on


__all__ = [
    "DISPOSITIONS",
    "HIDDEN",
    "OPERATOR",
    "PERCEIVABLE",
    "PLANTED",
    "PRIVATE_FIELDS",
    "RESTRICTED",
    "SEEN",
    "complete",
    "definition",
    "named",
    "require_primary_image",
    "visual_identity",
]
