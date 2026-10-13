"""Helpers for the catalog validation tests (#111): the dry run over REST, catalog characters
planted in #115's shape (labels, a content rating, a true age, images), rating policies linked
to a rating, row counts a dry run must leave alone, and fixture bodies.

The milestone parts take field names that are later milestones' lexicon terms (the scheduled
items a provider check reads, the module tracking starting values) from here: their bodies are
checked against the capability lexicon (docs/04 §13)."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ids import new_ulid
from epistrel.core.ratings import RatingReplacement, get_rating, update_rating
from tests.scenario_support import BASE, WRITE, plant_character, role, seed

PROVIDER = "weather_feed"
"""A provider the fixture's scheduled item names."""
PROVIDER_FIELD = "starting_state.scheduled_events[0].provider"
"""Where ``with_provider`` puts the provider's name."""

COUNTED = {
    "operations": "SELECT count(*) FROM operations",
    "catalog.scenario_versions": "SELECT count(*) FROM catalog.scenario_versions",
    "catalog.character_versions": "SELECT count(*) FROM catalog.character_versions",
    "idempotency_keys": "SELECT count(*) FROM idempotency_keys",
    "audit_log": "SELECT count(*) FROM audit_log",
    "config_changes": "SELECT count(*) FROM config_changes",
}
"""Tables a dry run must leave as they are, and how each is counted."""


async def validate(
    client: httpx.AsyncClient,
    scenario_id: str,
    body: Mapping[str, Any] | None = None,
    **params: Any,
) -> httpx.Response:
    """``POST /catalog/scenarios/{id}/validate`` (``params``: ``version``)."""
    return await client.post(
        f"{BASE}/{scenario_id}/validate",
        json=None if body is None else dict(body),
        params={k: str(v) for k, v in params.items()},
        headers=WRITE,
    )


async def counts(db: AsyncEngine) -> dict[str, int]:
    """The rows of every ``COUNTED`` table."""
    found: dict[str, int] = {}
    async with db.connect() as conn:
        for table, sql in COUNTED.items():
            found[table] = int(await conn.scalar(text(sql)) or 0)
    return found


async def rating_version(db: AsyncEngine, rating_id: str) -> str:
    """The current version id of ``rating_id``."""
    async with db.connect() as conn:
        found = await conn.scalar(
            text("SELECT current_version_id FROM content_ratings WHERE id = :r"), {"r": rating_id}
        )
    assert isinstance(found, str), rating_id
    return found


def character_document(
    name: str,
    *,
    rating_id: str | None = None,
    labels: Iterable[str] = (),
    true_age: int | None = None,
    images: Iterable[str] = (),
    personality: str = "Patient, private and slow to trust.",
) -> dict[str, Any]:
    """A character version's content in #115's planned shape."""
    gallery = list(images)
    document: dict[str, Any] = {
        "public_card": {"name": name, "images": {"gallery": gallery}},
        "metadata": {"labels": list(labels)},
        "definition": {"personality": personality},
    }
    if rating_id is not None:
        document["metadata"]["content_rating_id"] = rating_id
    if true_age is not None:
        document["definition"]["true_age"] = true_age
    return document


async def rated_character(
    client: httpx.AsyncClient,
    db: AsyncEngine,
    name: str,
    *,
    rating_id: str | None = None,
    labels: Iterable[str] = (),
    true_age: int | None = None,
    images: Iterable[str] = (),
) -> tuple[str, int]:
    """A catalog character created over REST whose published version is planted in #115's
    shape, recorded under its rating's current version: its id and version."""
    created = await client.post(
        "/catalog/characters", json={"name": name, "public_description": "Planted."}, headers=WRITE
    )
    assert created.status_code == 201, created.text
    character_id: str = created.json()["id"]
    version = await plant_character(
        db,
        character_id,
        character_document(
            name, rating_id=rating_id, labels=labels, true_age=true_age, images=images
        ),
        rating_version_id=None if rating_id is None else await rating_version(db, rating_id),
    )
    return character_id, version


async def list_policy(db: AsyncEngine, rating_id: str, policy_id: str) -> None:
    """Make ``rating_id`` list ``policy_id`` too (a new rating version)."""
    current = (await get_rating(db, rating_id)).rating
    await update_rating(
        db,
        rating_id,
        RatingReplacement(
            name=current.name,
            description=current.description,
            display_order=current.display_order,
            prompt_guidance=current.prompt_guidance,
            image_guidance=current.image_guidance,
            policy_ids=[*current.policy_ids, policy_id],
            inherits_from=list(current.inherits_from),
        ),
        principal="p-operator",
    )


async def link_rating_policy(
    client: httpx.AsyncClient, db: AsyncEngine, rating_id: str, *, applies_to: str | None = None
) -> str:
    """A new rating-scope word-list policy listed by ``rating_id`` (a new rating version): its
    id. ``applies_to`` ``text_input`` also sets it to apply to input."""
    body: dict[str, Any] = {
        "scope": "rating",
        "scope_id": rating_id,
        "name": f"rating-guard-{new_ulid().lower()}",
        "rule": {"type": "wordlist", "words": [f"zq{new_ulid().lower()}"]},
        "message": "Not at this rating.",
    }
    if applies_to is not None:
        body["applies_to"] = ["text_output", applies_to]
    made = await client.post(
        "/admin/policies", json=body, headers={**WRITE, "X-Epistrel-Principal": "p-operator"}
    )
    assert made.status_code == 201, made.text
    policy_id: str = made.json()["id"]
    await list_policy(db, rating_id, policy_id)
    return policy_id


async def global_wordlist(client: httpx.AsyncClient, word: str) -> str:
    """A new global word-list policy (it applies to input by default): its id."""
    made = await client.post(
        "/admin/policies",
        json={
            "scope": "global",
            "name": f"validation-{new_ulid().lower()}",
            "rule": {"type": "wordlist", "words": [word]},
            "message": "Please keep that word out of the story.",
        },
        headers={**WRITE, "X-Epistrel-Principal": "p-operator"},
    )
    assert made.status_code == 201, made.text
    policy_id: str = made.json()["id"]
    return policy_id


async def retire_policy(client: httpx.AsyncClient, policy_id: str) -> None:
    response = await client.delete(
        f"/admin/policies/{policy_id}", headers={**WRITE, "X-Epistrel-Principal": "p-operator"}
    )
    assert response.status_code in (200, 204), response.text


def filled(role_id: str, fill: str, character_id: str, **fields: Any) -> dict[str, Any]:
    """A ``locked`` or ``default`` role filled with catalog character ``character_id``."""
    return role(role_id, fill=fill, character={"catalog_id": character_id}, **fields)


def with_provider(
    body: Mapping[str, Any], name: str = PROVIDER, *, declared: Iterable[str] = ()
) -> dict[str, Any]:
    """``body`` with a scheduled item naming provider ``name`` (at ``PROVIDER_FIELD``) and
    ``declared`` as its required providers."""
    found = copy.deepcopy(dict(body))
    start = dict(found.get("starting_state", {}))
    start["scheduled_events"] = [{"at": "18:00", "what": "dinner", "provider": name}]
    found["starting_state"] = start
    found["story_settings_defaults"] = {"required_providers": list(declared)}
    return found


def incomplete_seed(text_: str = "The roof leaks.") -> dict[str, Any]:
    """A seed that declares its holder and disclosure but no audience."""
    found = seed(text_)
    del found["audience"]
    return found


def entries(report: Mapping[str, Any], code: str) -> list[dict[str, Any]]:
    """Every error and warning of ``report`` with ``code``."""
    return [e for e in (*report["errors"], *report["warnings"]) if e["code"] == code]


def codes(found: Iterable[Mapping[str, Any]]) -> list[tuple[str, str | None]]:
    """``(code, field)`` of each entry, in order."""
    return [(e["code"], e["location"]["field"]) for e in found]


__all__ = [
    "COUNTED",
    "PROVIDER",
    "PROVIDER_FIELD",
    "character_document",
    "codes",
    "counts",
    "entries",
    "filled",
    "global_wordlist",
    "incomplete_seed",
    "link_rating_policy",
    "list_policy",
    "rated_character",
    "rating_version",
    "retire_policy",
    "validate",
    "with_provider",
]
