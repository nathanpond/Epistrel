"""The rating-mismatch check (FR-RATE-5; #111): a looser item warns and never blocks.

Ratings are compared by their guardrails, never by display order. An item is **looser** than the
scenario when its rating's effective guardrails lack any policy the scenario's rating has
(``is_looser``: a set comparison of effective policy ids, #48's ``effective_guardrails``). Two
ratings that each have a policy the other lacks are each looser than the other. Only live
policies count: a retired one guards nothing.

The items are each ``locked`` and ``default`` role's character version and every media asset the
scenario shows (``checks.media``), each compared at the rating version it was stored with (the
character version's, else the rating its document names at the rating's current version; the
asset's own) against the scenario rating's current version: a rating edited since the item was
published is compared as it was then. An item without a rating has no guardrails. Each looser
item is a ``rating_looser`` warning (outcome ``kept``: it is used as it is, under the story's
rating's guardrails) at its field, naming it in ``source`` (``catalog_id@version``, or the
asset's id).

A scenario without a rating is the required check's to report (``content_rating_missing``), and
this check then has nothing to compare against.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from sqlalchemy import select

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.cast_members import filled
from epistrel.core.catalog.checks.media import assets, media_uses
from epistrel.core.catalog.checks.scenario import draft_of
from epistrel.core.catalog.report import Outcome
from epistrel.core.ratings import RatingNode, effective_guardrails, load_graph
from epistrel.core.tables import content_rating_versions, policies

RULE = (
    "rating: an item whose rating's guardrails lack a policy the scenario's rating has is "
    "looser; it plays under the story's rating (FR-RATE-5)"
)


def is_looser(item: Collection[str], scenario: Collection[str]) -> bool:
    """Whether an item with the effective guardrails ``item`` is looser than a scenario (or
    story) with ``scenario``: it lacks a policy the scenario has. Sets only; no order."""
    return not set(scenario) <= set(item)


class _Ratings:
    """The rating graph and the live policies, read once, and the effective guardrails of any
    rating at any of its versions."""

    def __init__(self, graph: Mapping[str, RatingNode], live: frozenset[str]) -> None:
        self.graph = graph
        self.live = live
        self.versions: dict[str, RatingNode | None] = {}

    def effective(self, rating_id: str | None, version: RatingNode | None = None) -> frozenset[str]:
        """The live effective guardrails of ``rating_id``, with ``version`` standing for its
        current version when given."""
        if rating_id is None:
            return frozenset()
        graph = self.graph if version is None else {**self.graph, rating_id: version}
        return frozenset(g.policy_id for g in effective_guardrails(rating_id, graph)) & self.live

    async def node(self, ctx: ValidationContext, rating_id: str, version_id: str) -> RatingNode:
        """``rating_id`` at its version ``version_id`` (its current node when that is it)."""
        current = self.graph.get(rating_id)
        if current is not None and current.version_id == version_id:
            return current
        if version_id not in self.versions:
            row = (
                await ctx.conn.execute(
                    select(content_rating_versions).where(
                        content_rating_versions.c.version_id == version_id
                    )
                )
            ).one_or_none()
            self.versions[version_id] = (
                None
                if row is None
                else RatingNode(
                    id=rating_id,
                    version_id=version_id,
                    name=row.name,
                    display_order=int(row.display_order),
                    policy_ids=tuple(row.policy_ids),
                    inherits_from=tuple(row.inherits_from),
                )
            )
        found = self.versions[version_id]
        return found if found is not None else _unknown(rating_id, version_id)

    async def stored(
        self, ctx: ValidationContext, rating_id: str | None, version_id: str | None
    ) -> frozenset[str]:
        """The live effective guardrails of an item stored with ``rating_id`` at
        ``version_id`` (the rating's current version when ``version_id`` is ``None``)."""
        if rating_id is None:
            return frozenset()
        if version_id is None:
            return self.effective(rating_id)
        return self.effective(rating_id, await self.node(ctx, rating_id, version_id))


def _unknown(rating_id: str, version_id: str) -> RatingNode:
    """A version no row holds (never written, or erased): no policy of its own, nothing
    inherited."""
    return RatingNode(
        id=rating_id,
        version_id=version_id,
        name=rating_id,
        display_order=0,
        policy_ids=(),
        inherits_from=(),
    )


async def _ratings(ctx: ValidationContext) -> _Ratings:
    graph = await load_graph(ctx.conn)
    live = frozenset(
        (await ctx.conn.scalars(select(policies.c.id).where(policies.c.retired_at.is_(None)))).all()
    )
    return _Ratings(graph, live)


async def _asset_ratings(ctx: ValidationContext, rows: Mapping[str, Any]) -> dict[str, str]:
    """Each asset's rating id, by the rating version it was stored with."""
    version_ids = sorted({str(row.content_rating_version_id) for row in rows.values()})
    if not version_ids:
        return {}
    found = await ctx.conn.execute(
        select(content_rating_versions.c.version_id, content_rating_versions.c.rating_id).where(
            content_rating_versions.c.version_id.in_(version_ids)
        )
    )
    return {str(row.version_id): str(row.rating_id) for row in found}


class RatingMismatchCheck(Check):
    """See the module docstring."""

    name = "rating_mismatch"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        scenario_rating = draft_of(subject).metadata.content_rating_id
        if scenario_rating is None:
            return Findings()
        ratings = await _ratings(ctx)
        guarded = ratings.effective(scenario_rating)
        found = Findings()
        for member in await filled(subject, ctx):
            if member.version is None:
                continue
            held = await ratings.stored(ctx, member.rating_id, member.rating_version_id)
            if is_looser(held, guarded):
                found.entries.append(
                    subject.entry(
                        "rating_looser",
                        Outcome.kept,
                        field=member.field,
                        role=member.role.id,
                        source=member.source,
                        rule=RULE,
                    )
                )
        rows = await assets(subject, ctx)
        heads = await _asset_ratings(ctx, rows)
        for use in await media_uses(subject, ctx):
            row = rows.get(use.media_id)
            if row is None:
                continue
            version_id = str(row.content_rating_version_id)
            held = await ratings.stored(ctx, heads.get(version_id), version_id)
            if is_looser(held, guarded):
                found.entries.append(
                    subject.entry(
                        "rating_looser",
                        Outcome.kept,
                        field=use.field,
                        role=use.role,
                        source=use.media_id,
                        rule=RULE,
                    )
                )
        return found


CHECK = RatingMismatchCheck()

__all__ = ["CHECK", "RatingMismatchCheck", "is_looser"]
