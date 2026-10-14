"""Content ratings: versioning, guardrail inheritance, and assignment (FR-RATE-1,2,4).

A rating is a head row plus immutable versions (``core.tables.ratings``). Every write — create,
edit, retire — runs under the deployment lock (``config_changes.lock_deployment``), advances the
deployment's ``config_revision`` by one and records a ``config_changes`` row (``rating:<id>``,
old/new version ids, class interpretive). An edit that changes nothing writes nothing.

**Effective guardrails** (FR-RATE-2) are a rating's own policies plus the effective guardrails
of every rating it inherits from, transitively, each policy once and tagged with the rating it
comes from. A policy reachable from several ratings is attributed to the nearest one (fewest
inheritance steps; ties by display order, name, id). Retired ratings stay in the graph: a rating
that inherits from one keeps its policies. Saving an inheritance cycle is refused
(``InheritanceCycle``). Prompt guidance is **not** inherited: a story's guidance is its own
rating's only (``story_guidance``).

The closure is computed on read and cached in-process per closure, keyed by the version id of
every rating in it — an edit to an inherited rating gives its descendants a new key — and the
cache is cleared on every rating write.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.config_changes import (
    RETIRED,
    bump_config_revision,
    lock_deployment,
    rating_setting,
    record_config_change,
)
from epistrel.core.errors import (
    ConfigConflict,
    InheritanceCycle,
    InvalidRequest,
    RatingNotFound,
    RatingRetired,
    StoryNotFound,
)
from epistrel.core.ids import new_ulid
from epistrel.core.tables import content_rating_versions, content_ratings, policies, stories

NAME_MAX = 80
DESCRIPTION_MAX = 4_000
GUIDANCE_MAX = 8_000
REFERENCE_MAX = 64
REFERENCES_MAX = 256
DISPLAY_ORDER_STEP = 10
"""A rating created without ``display_order`` goes after every live rating, by this step."""
DISPLAY_ORDER_MIN = -(2**31)
DISPLAY_ORDER_MAX = 2**31 - 1
"""The ``display_order`` column is ``INTEGER``: a value outside int32 is a 422, never a 500 (#280).
An assigned order stops at the maximum, where ties sort by name, then id."""

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)]
Reference = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=REFERENCE_MAX)
]
DisplayOrder = Annotated[int, Field(ge=DISPLAY_ORDER_MIN, le=DISPLAY_ORDER_MAX)]


class RatingFields(BaseModel):
    """What a caller supplies when creating a rating (``POST /admin/content-ratings``).

    ``policy_ids`` are the rating's own policies and must name live ``rating``-scoped policies
    (#49); ``inherits_from`` must name existing ratings. Duplicates are dropped,
    keeping the first occurrence.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "Teen",
                    "description": "Mild violence and language.",
                    "prompt_guidance": "Keep it suitable for 13+.",
                    "inherits_from": ["00000000000000000000RAT001"],
                }
            ]
        },
    )

    name: Name
    description: str = Field(default="", max_length=DESCRIPTION_MAX)
    display_order: DisplayOrder | None = None
    prompt_guidance: str = Field(default="", max_length=GUIDANCE_MAX)
    image_guidance: str = Field(default="", max_length=GUIDANCE_MAX)
    policy_ids: list[Reference] = Field(default_factory=list, max_length=REFERENCES_MAX)
    inherits_from: list[Reference] = Field(default_factory=list, max_length=REFERENCES_MAX)


class RatingReplacement(RatingFields):
    """A full replacement (``PUT /admin/content-ratings/{id}``): every field is required."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Teen",
                    "description": "Mild violence and language.",
                    "display_order": 20,
                    "prompt_guidance": "Keep it suitable for 13+.",
                    "image_guidance": "",
                    "policy_ids": [],
                    "inherits_from": ["00000000000000000000RAT001"],
                }
            ]
        }
    )

    description: str = Field(max_length=DESCRIPTION_MAX)
    display_order: DisplayOrder
    prompt_guidance: str = Field(max_length=GUIDANCE_MAX)
    image_guidance: str = Field(max_length=GUIDANCE_MAX)
    policy_ids: list[Reference] = Field(max_length=REFERENCES_MAX)
    inherits_from: list[Reference] = Field(max_length=REFERENCES_MAX)


@dataclass(frozen=True, slots=True)
class Rating:
    """A rating at its latest version."""

    id: str
    name: str
    description: str
    display_order: int
    prompt_guidance: str
    image_guidance: str
    policy_ids: tuple[str, ...]
    inherits_from: tuple[str, ...]
    version_id: str
    version: int
    created_at: datetime
    created_by: str
    retired_at: datetime | None


@dataclass(frozen=True, slots=True)
class Guardrail:
    """One effective policy of a rating and the rating it comes from."""

    policy_id: str
    source_rating_id: str


@dataclass(frozen=True, slots=True)
class RatingDetail:
    rating: Rating
    effective_guardrails: tuple[Guardrail, ...]


@dataclass(frozen=True, slots=True)
class RatingWrite:
    """The rating after a write and the deployment ``config_revision`` the write left."""

    rating: Rating
    config_revision: int
    changed: bool
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class StoryGuidance:
    """The guidance of a story's rating at its current version (FR-RATE-4): never inherited."""

    rating_id: str
    rating_version_id: str
    prompt_guidance: str
    image_guidance: str


# -- the pure part: closure and cycle check ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class RatingNode:
    """What the closure needs of one rating's current version."""

    id: str
    version_id: str
    name: str
    display_order: int
    policy_ids: tuple[str, ...]
    inherits_from: tuple[str, ...]

    @property
    def rank(self) -> tuple[int, str, str]:
        return (self.display_order, self.name.casefold(), self.id)


Graph = Mapping[str, RatingNode]

_CLOSURE_CACHE: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[Guardrail, ...]] = {}


def invalidate_closure_cache() -> None:
    """Forget every cached closure (called on every rating write)."""
    _CLOSURE_CACHE.clear()


def _layers(rating_id: str, graph: Graph) -> list[list[RatingNode]]:
    """The ratings reachable from ``rating_id`` by inheritance, by distance (0 = itself).

    Each layer is ordered by display order, name and id; a rating appears once, at its
    shortest distance. Ids missing from ``graph`` are skipped.
    """
    if rating_id not in graph:
        return []
    seen = {rating_id}
    layers = [[graph[rating_id]]]
    while True:
        following: dict[str, RatingNode] = {}
        for node in layers[-1]:
            for parent in node.inherits_from:
                if parent in graph and parent not in seen:
                    following[parent] = graph[parent]
        if not following:
            return layers
        seen.update(following)
        layers.append(sorted(following.values(), key=lambda n: n.rank))


def effective_guardrails(rating_id: str, graph: Graph) -> tuple[Guardrail, ...]:
    """Own policies plus the effective guardrails of every inherited rating, each policy once,
    ordered by source display order (then name, id) and policy id."""
    layers = _layers(rating_id, graph)
    key = (rating_id, _versions(layers))
    cached = _CLOSURE_CACHE.get(key)
    if cached is not None:
        return cached
    sources: dict[str, RatingNode] = {}
    for layer in layers:
        for node in layer:
            for policy_id in node.policy_ids:
                sources.setdefault(policy_id, node)
    result = tuple(
        Guardrail(policy_id=policy_id, source_rating_id=node.id)
        for policy_id, node in sorted(sources.items(), key=lambda item: (item[1].rank, item[0]))
    )
    _CLOSURE_CACHE[key] = result
    return result


def _versions(layers: list[list[RatingNode]]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((node.id, node.version_id) for layer in layers for node in layer))


def closure_versions(rating_id: str, graph: Graph) -> tuple[tuple[str, str], ...]:
    """``(rating id, version id)`` of every rating in ``rating_id``'s closure, sorted: changes
    whenever any rating the closure depends on is edited (the policy store's cache key)."""
    return _versions(_layers(rating_id, graph))


def find_cycle(rating_id: str, inherits_from: Sequence[str], graph: Graph) -> list[str] | None:
    """The inheritance loop saving ``rating_id`` with ``inherits_from`` would create, as rating
    ids from ``rating_id`` back to it, or ``None`` when there is none."""
    path: list[str] = [rating_id]
    on_path: set[str] = set()
    done: set[str] = set()

    def visit(node_id: str) -> list[str] | None:
        if node_id == rating_id:
            return [*path, rating_id]
        if node_id in done or node_id in on_path or node_id not in graph:
            return None
        on_path.add(node_id)
        path.append(node_id)
        for parent in graph[node_id].inherits_from:
            found = visit(parent)
            if found is not None:
                return found
        path.pop()
        on_path.discard(node_id)
        done.add(node_id)
        return None

    for parent in inherits_from:
        found = visit(parent)
        if found is not None:
            return found
    return None


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


# -- reads ------------------------------------------------------------------------------------

_VERSION_COLUMNS = (
    content_rating_versions.c.name,
    content_rating_versions.c.description,
    content_rating_versions.c.display_order,
    content_rating_versions.c.prompt_guidance,
    content_rating_versions.c.image_guidance,
    content_rating_versions.c.policy_ids,
    content_rating_versions.c.inherits_from,
    content_rating_versions.c.version_id,
    content_rating_versions.c.version,
    content_rating_versions.c.created_at,
    content_rating_versions.c.created_by,
)


def _current() -> Any:
    return select(content_ratings.c.id, content_ratings.c.retired_at, *_VERSION_COLUMNS).join(
        content_rating_versions,
        content_rating_versions.c.version_id == content_ratings.c.current_version_id,
    )


def _rating(row: Any, *, rating_id: str | None = None, retired_at: Any = None) -> Rating:
    mapping = row._mapping
    return Rating(
        id=rating_id if rating_id is not None else mapping["id"],
        name=mapping["name"],
        description=mapping["description"],
        display_order=int(mapping["display_order"]),
        prompt_guidance=mapping["prompt_guidance"],
        image_guidance=mapping["image_guidance"],
        policy_ids=tuple(mapping["policy_ids"]),
        inherits_from=tuple(mapping["inherits_from"]),
        version_id=mapping["version_id"],
        version=int(mapping["version"]),
        created_at=mapping["created_at"],
        created_by=mapping["created_by"],
        retired_at=retired_at if rating_id is not None else mapping["retired_at"],
    )


def _node(rating: Rating) -> RatingNode:
    return RatingNode(
        id=rating.id,
        version_id=rating.version_id,
        name=rating.name,
        display_order=rating.display_order,
        policy_ids=rating.policy_ids,
        inherits_from=rating.inherits_from,
    )


async def _all(conn: AsyncConnection) -> list[Rating]:
    return [_rating(row) for row in (await conn.execute(_current())).all()]


async def load_graph(conn: AsyncConnection) -> dict[str, RatingNode]:
    """Every rating's current version, retired ones included (their policies still apply)."""
    return {rating.id: _node(rating) for rating in await _all(conn)}


async def _one(conn: AsyncConnection, rating_id: str) -> Rating:
    row = (await conn.execute(_current().where(content_ratings.c.id == rating_id))).one_or_none()
    if row is None:
        raise RatingNotFound(f"content rating {rating_id} does not exist", {"id": rating_id})
    return _rating(row)


def _sorted(ratings: Iterable[Rating]) -> list[Rating]:
    return sorted(ratings, key=lambda r: (r.display_order, r.name.casefold(), r.id))


async def list_ratings(db: AsyncEngine, *, include_retired: bool = False) -> list[Rating]:
    """Ratings by display order (ties by name); retired ones only on request."""
    async with db.connect() as conn:
        ratings = await _all(conn)
    return _sorted(r for r in ratings if include_retired or r.retired_at is None)


async def get_rating(db: AsyncEngine, rating_id: str) -> RatingDetail:
    """The latest version and its effective guardrails (a retired rating is still readable)."""
    async with db.connect() as conn, conn.begin():
        rating = await _one(conn, rating_id)
        graph = await load_graph(conn)
    return RatingDetail(rating=rating, effective_guardrails=effective_guardrails(rating.id, graph))


async def rating_versions(db: AsyncEngine, rating_id: str) -> list[Rating]:
    """Every version of a rating, newest first."""
    async with db.connect() as conn:
        head = (
            await conn.execute(
                select(content_ratings.c.retired_at).where(content_ratings.c.id == rating_id)
            )
        ).one_or_none()
        if head is None:
            raise RatingNotFound(f"content rating {rating_id} does not exist", {"id": rating_id})
        found = await conn.execute(
            select(*_VERSION_COLUMNS)
            .where(content_rating_versions.c.rating_id == rating_id)
            .order_by(content_rating_versions.c.version.desc())
        )
        return [_rating(row, rating_id=rating_id, retired_at=head.retired_at) for row in found]


async def require_assignable(
    conn: AsyncConnection, rating_id: str, *, field: str = "content_rating_id"
) -> str:
    """``rating_id`` if it can be newly assigned: 422 ``rating_retired`` when it is retired or
    unknown. Holds the head row ``FOR SHARE`` so it cannot be retired before the caller commits.
    """
    row = (
        await conn.execute(
            select(content_ratings.c.retired_at)
            .where(content_ratings.c.id == rating_id)
            .with_for_update(read=True)
        )
    ).one_or_none()
    if row is None or row.retired_at is not None:
        reason = "unknown" if row is None else "retired"
        raise RatingRetired(
            f"content rating {rating_id} cannot be assigned ({reason})",
            {"rating_id": rating_id, "field": field, "reason": reason},
        )
    return rating_id


async def story_guidance(db: AsyncEngine | AsyncConnection, story_id: str) -> StoryGuidance:
    """The prompt and image guidance of ``story_id``'s rating, at its current version."""
    query = (
        select(
            stories.c.content_rating_id,
            content_rating_versions.c.version_id,
            content_rating_versions.c.prompt_guidance,
            content_rating_versions.c.image_guidance,
        )
        .join(content_ratings, content_ratings.c.id == stories.c.content_rating_id)
        .join(
            content_rating_versions,
            content_rating_versions.c.version_id == content_ratings.c.current_version_id,
        )
        .where(stories.c.id == story_id)
    )
    if isinstance(db, AsyncConnection):
        row = (await db.execute(query)).one_or_none()
    else:
        async with db.connect() as conn:
            row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    return StoryGuidance(
        rating_id=row.content_rating_id,
        rating_version_id=row.version_id,
        prompt_guidance=row.prompt_guidance,
        image_guidance=row.image_guidance,
    )


# -- writes -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Content:
    name: str
    description: str
    display_order: int
    prompt_guidance: str
    image_guidance: str
    policy_ids: tuple[str, ...]
    inherits_from: tuple[str, ...]

    @classmethod
    def of(cls, rating: Rating) -> _Content:
        return cls(
            name=rating.name,
            description=rating.description,
            display_order=rating.display_order,
            prompt_guidance=rating.prompt_guidance,
            image_guidance=rating.image_guidance,
            policy_ids=rating.policy_ids,
            inherits_from=rating.inherits_from,
        )


async def _validate_policies(conn: AsyncConnection, added: Sequence[str]) -> None:
    """Every newly listed policy must be a live ``rating``-scoped policy (#49).

    Only ids the version adds are checked, so a rating listing a policy retired since can
    still be edited (a retired policy drops out of effective sets anyway).
    """
    if not added:
        return
    found = set(
        (
            await conn.scalars(
                select(policies.c.id).where(
                    policies.c.id.in_(list(added)),
                    policies.c.scope == "rating",
                    policies.c.retired_at.is_(None),
                )
            )
        ).all()
    )
    unknown = [policy_id for policy_id in added if policy_id not in found]
    if unknown:
        raise InvalidRequest(
            "policy_ids names policies that are not live rating policies",
            {"field": "body.policy_ids", "unknown": unknown},
        )


async def _validate(
    conn: AsyncConnection,
    rating_id: str,
    content: _Content,
    graph: Graph,
    previous: Sequence[str] = (),
) -> None:
    await _validate_policies(conn, [p for p in content.policy_ids if p not in previous])
    unknown = [parent for parent in content.inherits_from if parent not in graph]
    if unknown:
        raise InvalidRequest(
            "inherits_from names ratings that do not exist",
            {"field": "body.inherits_from", "unknown": unknown},
        )
    cycle = find_cycle(rating_id, content.inherits_from, graph)
    if cycle is not None:
        raise InheritanceCycle(
            "the rating would inherit from itself", {"field": "body.inherits_from", "cycle": cycle}
        )
    clash = await conn.scalar(
        select(content_ratings.c.id).where(
            func.lower(content_ratings.c.name) == content.name.lower(),
            content_ratings.c.retired_at.is_(None),
            content_ratings.c.id != rating_id,
        )
    )
    if clash is not None:
        raise InvalidRequest(
            "another live rating has this name", {"field": "body.name", "conflict": clash}
        )


async def _insert_version(
    conn: AsyncConnection, rating_id: str, version: int, content: _Content, principal: str
) -> str:
    version_id = new_ulid()
    await conn.execute(
        insert(content_rating_versions).values(
            version_id=version_id,
            rating_id=rating_id,
            version=version,
            name=content.name,
            description=content.description,
            display_order=content.display_order,
            prompt_guidance=content.prompt_guidance,
            image_guidance=content.image_guidance,
            policy_ids=list(content.policy_ids),
            inherits_from=list(content.inherits_from),
            created_by=principal,
        )
    )
    return version_id


async def create_rating(db: AsyncEngine, fields: RatingFields, *, principal: str) -> RatingWrite:
    """Create a rating at version 1 (``config_changes``: old ``null``, new the version id)."""
    rating_id = new_ulid()
    async with db.begin() as conn:
        state = await lock_deployment(conn)
        ratings = await _all(conn)
        graph = {r.id: _node(r) for r in ratings}
        display_order = fields.display_order
        if display_order is None:
            live = [r.display_order for r in ratings if r.retired_at is None]
            display_order = min(max(live, default=0) + DISPLAY_ORDER_STEP, DISPLAY_ORDER_MAX)
        content = _Content(
            name=fields.name,
            description=fields.description,
            display_order=display_order,
            prompt_guidance=fields.prompt_guidance,
            image_guidance=fields.image_guidance,
            policy_ids=_unique(fields.policy_ids),
            inherits_from=_unique(fields.inherits_from),
        )
        await _validate(conn, rating_id, content, graph)
        version_id = await _insert_version(conn, rating_id, 1, content, principal)
        await conn.execute(
            insert(content_ratings).values(
                id=rating_id, name=content.name, current_version_id=version_id
            )
        )
        revision = await bump_config_revision(conn, state)
        await record_config_change(
            conn,
            config_revision=revision,
            setting=rating_setting(rating_id),
            old=None,
            new=version_id,
            actor=principal,
        )
        rating = await _one(conn, rating_id)
    invalidate_closure_cache()
    return RatingWrite(rating=rating, config_revision=revision, changed=True)


async def update_rating(
    db: AsyncEngine, rating_id: str, fields: RatingReplacement, *, principal: str
) -> RatingWrite:
    """Replace every field as a new version; identical content writes nothing.

    A retired rating cannot be edited (409, ``reason = retired``).
    """
    async with db.begin() as conn:
        state = await lock_deployment(conn)
        current = await _one(conn, rating_id)
        if current.retired_at is not None:
            raise ConfigConflict(
                f"content rating {rating_id} is retired", {"id": rating_id, "reason": "retired"}
            )
        content = _Content(
            name=fields.name,
            description=fields.description,
            display_order=fields.display_order,
            prompt_guidance=fields.prompt_guidance,
            image_guidance=fields.image_guidance,
            policy_ids=_unique(fields.policy_ids),
            inherits_from=_unique(fields.inherits_from),
        )
        if content == _Content.of(current):
            return RatingWrite(rating=current, config_revision=state.config_revision, changed=False)
        await _validate(
            conn, rating_id, content, await load_graph(conn), previous=current.policy_ids
        )
        version_id = await _insert_version(conn, rating_id, current.version + 1, content, principal)
        await conn.execute(
            update(content_ratings)
            .where(content_ratings.c.id == rating_id)
            .values(name=content.name, current_version_id=version_id)
        )
        revision = await bump_config_revision(conn, state)
        await record_config_change(
            conn,
            config_revision=revision,
            setting=rating_setting(rating_id),
            old=current.version_id,
            new=version_id,
            actor=principal,
        )
        rating = await _one(conn, rating_id)
    invalidate_closure_cache()
    return RatingWrite(rating=rating, config_revision=revision, changed=True)


async def retire_rating(db: AsyncEngine, rating_id: str, *, principal: str) -> RatingWrite:
    """Retire a rating: hidden from listings and new assignment, kept for what uses it.

    The deployment default cannot be retired (409, ``reason = is_default``); retiring a retired
    rating changes nothing (``changed`` is false).
    """
    async with db.begin() as conn:
        state = await lock_deployment(conn)
        current = await _one(conn, rating_id)
        if current.retired_at is not None:
            return RatingWrite(rating=current, config_revision=state.config_revision, changed=False)
        if state.default_content_rating_id == rating_id:
            raise ConfigConflict(
                f"content rating {rating_id} is the deployment default",
                {"id": rating_id, "reason": "is_default"},
            )
        await conn.execute(
            update(content_ratings)
            .where(content_ratings.c.id == rating_id)
            .values(retired_at=func.transaction_timestamp(), retired_by=principal)
        )
        revision = await bump_config_revision(conn, state)
        await record_config_change(
            conn,
            config_revision=revision,
            setting=rating_setting(rating_id),
            old=current.version_id,
            new=RETIRED,
            actor=principal,
        )
        rating = await _one(conn, rating_id)
    invalidate_closure_cache()
    return RatingWrite(rating=rating, config_revision=revision, changed=True)
