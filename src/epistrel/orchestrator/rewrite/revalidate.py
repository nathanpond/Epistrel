"""Revalidating a rewrite at its final commit (FR-CONC-7,13; #237).

A session holds no lock while the author reads and answers: play goes on. The apply runs
``revalidate()`` as the apply operation's first commit step, so under the story row's
``FOR UPDATE``, and only there (never at answer or approval time). It compares the story with
what the plan was computed against (its stored read set, extended at plan time by
``snapshot()``):

- **erasure** (``Abandon``): a story-scope erasure committed since the plan (one its snapshot
  did not list) whose subject the plan references (``entity_ids``); checked even when the
  revision has not moved, and before the apply prepares anything. Copies a fork made (no
  revision in this story) are skipped; catalog-scope erasures never abandon a
  rewrite (stories keep their own copies, FR-STORE-14);
- ``config_changed``: an interpretive configuration change of the story's or the deployment's
  after the plan's high-water revisions (#64), or a changed clock anchor (#195);
- ``proposition_changed``: #136's ``proposition_changes`` since the base on a
  ``(subject, predicate)`` the plan changes;
- ``state_changed``: committed ``state_changes`` (any status) or ``external_values`` versions
  since the base on a ``(state_id, scope_key)`` the plan writes (M6's State writes record no
  ``proposition_changes``);
- ``membership_changed`` / ``record_changed``: a planned record's operation changed membership
  (a truncate, untruncate or swap), or #236's ``record_version`` of it moved (an amendment
  corrected it);
- ``ownership_changed`` (#697): a planned State's owner, its provider or the provider's write
  capability, or a planned predicate's registry owner, changed since the plan (checked even
  when the revision has not moved);
- ``new_dependent`` (FR-CONC-13): the planner, re-run under the lock, finds a dependent the
  preview did not have (a belief formed meanwhile from the old fact). Skipped under the fault
  point ``rewrite.revalidate.skip_research`` (``RESEARCH``).

Every check runs (cheapest first) and all reasons are returned, each with at most ``MAX_REFS``
``{table, id}`` refs; only ``Abandon`` short-circuits. Nothing found → ``Fresh``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import world_clock
from epistrel.core.erasure.guard import story_erasures
from epistrel.core.input_versions import _operation_versions
from epistrel.core.tables import (
    config_changes,
    external_values,
    metadata,
    operations,
    proposition_changes,
    state_changes,
)

MAX_REFS = 50
RESEARCH = True
"""``False`` under the fault point ``rewrite.revalidate.skip_research`` (a test build)."""


@dataclass(frozen=True)
class Fresh:
    """Nothing the plan read has changed: apply."""


@dataclass(frozen=True)
class Stale:
    """The plan must be recomputed: the reasons, each ``{kind, refs}``."""

    reasons: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Abandon:
    """The rewrite cannot go on (an erasure touched what it reads)."""

    reason: str
    erasure_id: str


Outcome = Fresh | Stale | Abandon
Replan = Callable[[AsyncConnection], Awaitable[Mapping[str, Any] | None]]
"""Re-run the planner at the current state; the new read set (``None``: no plan)."""


def _planned_records(read_set: Mapping[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for item in [*(read_set.get("records") or []), *(read_set.get("dependents") or [])]:
        ref = (str(item["table"]), str(item["id"]))
        if ref not in found:
            found.append(ref)
    return found


async def _operations_of(
    conn: AsyncConnection, story_id: str, records: Sequence[tuple[str, str]]
) -> dict[str, list[dict[str, str]]]:
    """Per operation id, the planned records it wrote (tables without ``operation_id`` are
    left out)."""
    by_table: dict[str, list[str]] = {}
    for table, record_id in records:
        by_table.setdefault(table, []).append(record_id)
    found: dict[str, list[dict[str, str]]] = {}
    for table, ids in by_table.items():
        t = metadata.tables.get(table)
        if t is None or "operation_id" not in t.c or "id" not in t.c:
            continue
        # #784: the story names its partition of a partitioned table (``memories``).
        own = [t.c.story_id == story_id] if "story_id" in t.c else []
        rows = await conn.execute(select(t.c.id, t.c.operation_id).where(t.c.id.in_(ids), *own))
        for record_id, operation_id in rows:
            if operation_id is not None:
                found.setdefault(str(operation_id), []).append(
                    {"table": table, "id": str(record_id)}
                )
    return found


async def _config_high_water(conn: AsyncConnection, story_id: str) -> dict[str, int]:
    c = config_changes.c
    story = await conn.scalar(
        select(func.max(c.config_revision)).where(c.scope == "story", c.scope_id == story_id)
    )
    deployment = await conn.scalar(
        select(func.max(c.config_revision)).where(c.scope == "deployment")
    )
    return {"story": int(story or 0), "deployment": int(deployment or 0)}


def _keys(changes: Iterable[Mapping[str, Any]]) -> tuple[list[list[str]], list[list[str]]]:
    props: list[list[str]] = []
    states: list[list[str]] = []
    for change in changes:
        if change.get("kind") == "proposition" and change.get("subject_id"):
            pair = [str(change["subject_id"]), str(change.get("predicate_id") or "")]
            if any(v.startswith(("new:", "register:")) or not v for v in pair):
                continue  # an entity or predicate the apply creates: nothing can change it
            if pair not in props:
                props.append(pair)
        elif change.get("kind") == "state" and change.get("state_id"):
            pair = [str(change["state_id"]), str(change.get("scope_key") or "")]
            if pair not in states:
                states.append(pair)
    return props, states


async def snapshot(
    conn: AsyncConnection,
    story_id: str,
    read_set: Mapping[str, Any],
    changes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """What ``revalidate`` compares against, taken with the plan: the configuration
    high-water revisions, the clock anchor's revision, the planned records' operation versions
    (membership and #236's record version), and the proposition and State keys."""
    ops = await _operations_of(conn, story_id, _planned_records(read_set))
    versions = await _operation_versions(conn, list(ops))
    props, states = _keys(changes)
    return {
        "erasures": sorted(e.erasure_id for e in await story_erasures(conn, story_id)),
        "config_revisions": {
            **dict(read_set.get("config_revisions") or {}),
            **await _config_high_water(conn, story_id),
        },
        "anchor_revision": await world_clock.anchor_revision(conn, story_id),
        "operations": {op: list(versions.get(op) or []) for op in ops},
        "prop_keys": props,
        "state_keys": states,
        "ownership": await _ownership(conn, story_id, states, props),
    }


async def _ownership(
    conn: AsyncConnection,
    story_id: str,
    states: Sequence[Sequence[str]],
    props: Sequence[Sequence[str]],
) -> dict[str, list[Any]]:
    """#697: who owns what the plan changes: each State's owner, its provider and whether the
    provider takes writes, and each predicate's registry owner. An owner or write capability
    that changes before the apply makes the plan stale (``ownership_changed``)."""
    from epistrel.core import providers as core_providers
    from epistrel.core.propositions.registry import load
    from epistrel.core.tables import state_definitions

    found: dict[str, list[Any]] = {}
    d = state_definitions.c
    for state_id in dict.fromkeys(str(s[0]) for s in states):
        row = (
            await conn.execute(select(d.owner, d.provider_id).where(d.id == state_id).limit(1))
        ).one_or_none()
        if row is None:
            continue
        writable = None
        if row.provider_id is not None:
            provider = await core_providers.find_provider(conn, str(row.provider_id))
            writable = None if provider is None else bool(provider.write_capable)
        found[f"state:{state_id}"] = [
            str(row.owner),
            None if row.provider_id is None else str(row.provider_id),
            writable,
        ]
    if props:
        registry = await load(conn, story_id)
        for predicate_id in dict.fromkeys(str(p[1]) for p in props):
            predicate = registry.get(predicate_id)
            if predicate is not None:
                found[f"predicate:{predicate_id}"] = [
                    str(getattr(predicate.owner, "value", predicate.owner))
                ]
    return found


def _refs(items: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for item in items:
        if item not in found:
            found.append(item)
        if len(found) >= MAX_REFS:
            break
    return found


async def erased(
    conn: AsyncConnection, story_id: str, read_set: Mapping[str, Any]
) -> Abandon | None:
    """``Abandon`` when a story-scope erasure committed in this story since the plan (one the
    snapshot did not list) erased a subject the plan references; a fork's copies (no revision
    in this story) are skipped."""
    known = {str(e) for e in read_set.get("erasures") or []}
    entity_ids = {str(e) for e in read_set.get("entity_ids") or []}
    for found in await story_erasures(conn, story_id):
        if found.erasure_id in known or found.revision is None:
            continue
        if found.ids & entity_ids:
            return Abandon("erased_subject_referenced", found.erasure_id)
    return None


async def revalidate(
    conn: AsyncConnection,
    story_id: str,
    *,
    base_revision: int,
    current_revision: int,
    read_set: Mapping[str, Any],
    replan: Replan,
) -> Outcome:
    """The outcome of rechecking the plan's read set (see the module docstring); run under the
    story row lock."""
    abandon = await erased(conn, story_id, read_set)
    if abandon is not None:
        return abandon
    reasons: list[dict[str, Any]] = []

    # Configuration: an interpretive change after the plan's high-water revisions, or the anchor.
    stored = dict(read_set.get("config_revisions") or {})
    c = config_changes.c
    changed = list(
        await conn.execute(
            select(c.change_id, c.setting).where(
                c["class"] == "interpretive",
                or_(
                    and_(
                        c.scope == "story",
                        c.scope_id == story_id,
                        c.config_revision > int(stored.get("story", 0)),
                    ),
                    and_(
                        c.scope == "deployment",
                        c.config_revision > int(stored.get("deployment", 0)),
                    ),
                ),
            )
        )
    )
    refs = [{"table": "config_changes", "id": str(row.change_id)} for row in changed]
    anchor = read_set.get("anchor_revision")
    if anchor is not None and await world_clock.anchor_revision(conn, story_id) != anchor:
        refs.append({"table": "stories", "id": story_id})
    if refs:
        reasons.append({"kind": "config_changed", "refs": _refs(refs)})

    # #697: an owner or a provider's write capability changed since the plan (neither moves
    # the revision).
    owned = dict(read_set.get("ownership") or {})
    if owned:
        states_now = [k.split(":", 1)[1] for k in owned if k.startswith("state:")]
        props_now = [["", k.split(":", 1)[1]] for k in owned if k.startswith("predicate:")]
        now = await _ownership(conn, story_id, [[s] for s in states_now], props_now)
        moved_refs = [
            {
                "table": "state_definitions" if key.startswith("state:") else "predicates",
                "id": key.split(":", 1)[1],
            }
            for key, was in owned.items()
            if list(now.get(key) or []) != list(was)
        ]
        if moved_refs:
            reasons.append({"kind": "ownership_changed", "refs": _refs(moved_refs)})

    if current_revision == base_revision:
        return Stale(reasons) if reasons else Fresh()

    # Propositions the plan changes, changed since the base (#136's change index).
    props = [tuple(p) for p in read_set.get("prop_keys") or []]
    if props:
        pc = proposition_changes.c
        hit = await conn.execute(
            select(pc.id).where(
                pc.story_id == story_id,
                pc.timeline_revision > base_revision,
                tuple_(pc.subject_id, pc.predicate_id).in_(props),
            )
        )
        refs = [{"table": "proposition_changes", "id": str(r.id)} for r in hit]
        if refs:
            reasons.append({"kind": "proposition_changed", "refs": _refs(refs)})

    # States the plan writes, changed since the base (M6's writes bypass the change index).
    states = [(s, k) for s, k in read_set.get("state_keys") or []]
    if states:
        o = operations.c
        sc = state_changes.c
        wanted = or_(
            *(
                and_(
                    sc.state_id == state_id,
                    sc.scope_key.is_(None) if not scope else sc.scope_key == scope,
                )
                for state_id, scope in states
            )
        )
        moved = await conn.execute(
            select(sc.id)
            .select_from(state_changes.join(operations, o.operation_id == sc.operation_id))
            .where(sc.story_id == story_id, o.timeline_revision > base_revision, wanted)
        )
        refs = [{"table": "state_changes", "id": str(r.id)} for r in moved]
        ev = external_values.c
        external = await conn.execute(
            select(ev.event_id)
            .select_from(external_values.join(operations, o.operation_id == ev.operation_id))
            .where(
                ev.story_id == story_id,
                o.timeline_revision > base_revision,
                or_(
                    *(
                        and_(ev.state_id == state_id, ev.scope_key == (scope or ""))
                        for state_id, scope in states
                    )
                ),
            )
        )
        refs += [{"table": "external_values", "id": str(r.event_id)} for r in external]
        if refs:
            reasons.append({"kind": "state_changed", "refs": _refs(refs)})

    # Planned records: their operations' membership and #236's record version.
    before: dict[str, list[Any]] = dict(read_set.get("operations") or {})
    if before:
        ops = await _operations_of(conn, story_id, _planned_records(read_set))
        now = await _operation_versions(conn, list(before))
        membership: list[dict[str, str]] = []
        record: list[dict[str, str]] = []
        for op, was in before.items():
            current = list(now.get(op) or [])
            written = ops.get(op) or [{"table": "operations", "id": op}]
            if current[:2] != list(was)[:2]:
                membership += written
            elif current[2:] != list(was)[2:]:
                record += written
        if membership:
            reasons.append({"kind": "membership_changed", "refs": _refs(membership)})
        if record:
            reasons.append({"kind": "record_changed", "refs": _refs(record)})

    # The dependency search again (FR-CONC-13).
    if RESEARCH:
        again = await replan(conn)
        if again is not None:
            known = {(str(d["table"]), str(d["id"])) for d in read_set.get("dependents") or []}
            new = [
                {"table": str(d["table"]), "id": str(d["id"])}
                for d in again.get("dependents") or []
                if (str(d["table"]), str(d["id"])) not in known
            ]
            if new:
                reasons.append({"kind": "new_dependent", "refs": _refs(new)})
    return Stale(reasons) if reasons else Fresh()


__all__ = [
    "MAX_REFS",
    "RESEARCH",
    "Abandon",
    "Fresh",
    "Outcome",
    "Replan",
    "Revalidated",
    "Stale",
    "erased",
    "revalidate",
    "snapshot",
]


class Revalidated(Exception):
    """Raised by the apply's commit step when the outcome is not ``Fresh``: the apply's
    transaction rolls back, and the session handles ``outcome``."""

    def __init__(self, outcome: Stale | Abandon) -> None:
        super().__init__(type(outcome).__name__)
        self.outcome = outcome
