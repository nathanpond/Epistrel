"""Author-directed regeneration (FR-COMMIT-12, FR-COMMIT-14; #241).

On a regeneration the author can direct the outcome ("the knocker is George, her ex"). Author
authority overrides the insignificance presumption of FR-COMMIT-11, but never the
reconciliation check of FR-COMMIT-14:

- ``prepare`` parses the direction (``directive_parse``): its steer, and a ``binding`` naming
  who or what an open question of the scene is. The binding is matched to the latest open thunk
  the regenerated turn reads (one the replaced variant resolved reads as open again), its open
  slot filled with the named entity (created when the story has none by that name: the
  author's permission), at the thunk's original time (``lifecycle.binding_of``). Then
  ``reconcile`` always runs.
- **Ok** (``directing``): the regeneration is played with "Author decision: …" in place of the
  deferral bias (``DECISION``, read by ``structured.pipeline._steering``), the resolution screen
  never re-steers the directed thunk, and its resolution commits with ``authority = author``
  (``lifecycle.AUTHOR_DIRECTED``). No rewrite session, no amendment.
- **Conflict**: the author's choice stands, but proceeds as an amendment
  (``rewrite.session.open_directed``: a ``directed_resolution`` session whose changes are the
  binding's implied obligations at the thunk's time and whose conflicts are the conflicting
  records and the bound character's later lines). Applied, the amendment commits and the
  directed regeneration runs; the resolution then commits even though the recorded history it
  settled conflicted (``lifecycle.AUTHOR_DIRECTED``).

A direction with no binding, or a binding that matches no open thunk, is a steer for this
regeneration only (warning ``binding_unmatched``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.commit import lifecycle
from epistrel.core.commit.reconcile import Conflict, Ok, reconcile
from epistrel.core.enums import OpenSlot
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Proposition
from epistrel.core.tables import entities, entity_aliases, operations, thunks
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.rewrite.api import RewriteSession

log = logging.getLogger(__name__)

DECISION: ContextVar[str | None] = ContextVar("epistrel_author_decision", default=None)
"""The "Author decision" steer of the regeneration being played (``None``: not directed)."""
BINDING_UNMATCHED = "binding_unmatched"


@dataclass(frozen=True)
class Directed:
    """A prepared direction: its text, the steer the renders carry, and, when it binds an open
    thunk, the thunk, the entity, the binding and the reconciliation's answer."""

    text: str
    steer: str
    thunk_id: str | None = None
    entity_id: str | None = None
    binding: Proposition | None = None
    reconciled: Ok | Conflict | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def conflicting(self) -> bool:
        return isinstance(self.reconciled, Conflict)

    def document(self) -> dict[str, Any]:
        """What a ``directed_resolution`` session stores to replay the direction at apply."""
        return {
            "text": self.text,
            "steer": self.steer,
            "thunk_id": self.thunk_id,
            "entity_id": self.entity_id,
            "binding": None if self.binding is None else self.binding.model_dump(mode="json"),
        }

    @classmethod
    def of(cls, document: dict[str, Any]) -> Directed:
        binding = document.get("binding")
        return cls(
            text=str(document["text"]),
            steer=str(document["steer"]),
            thunk_id=document.get("thunk_id"),
            entity_id=document.get("entity_id"),
            binding=None if binding is None else Proposition.model_validate(binding),
        )


@contextmanager
def directing(directed: Directed) -> Iterator[None]:
    """Play a regeneration under ``directed``: its steer, and its thunk author-directed."""
    decision = DECISION.set(directed.steer)
    thunked = lifecycle.AUTHOR_DIRECTED.set(
        frozenset({directed.thunk_id}) if directed.thunk_id else frozenset()
    )
    try:
        yield
    finally:
        lifecycle.AUTHOR_DIRECTED.reset(thunked)
        DECISION.reset(decision)


async def _entity(conn: AsyncConnection, story_id: str, name: str) -> str:
    """The story's entity by name or alias (case-insensitive), created when there is none."""
    wanted = name.strip().casefold()
    e, a = entities.c, entity_aliases.c
    found = await conn.scalar(
        select(e.id)
        .where(
            e.story_id == story_id,
            e.merged_into.is_(None),
            or_(
                func.lower(e.canonical_name) == wanted,
                e.id.in_(
                    select(a.entity_id).where(a.story_id == story_id, func.lower(a.alias) == wanted)
                ),
            ),
        )
        .order_by(e.created_at, e.id)
        .limit(1)
    )
    if found is not None:
        return str(found)
    return str(await store.add_entity(conn, story_id, "person", name.strip()))


async def _thunk(conn: AsyncConnection, story_id: str) -> lifecycle.Thunk | None:
    """The latest thunk the regenerated (head) turn reads open: open now, or resolved by the
    head turn's active variant (its regeneration reopens it), with an open subject or object
    slot."""
    t = thunks.alias("t")
    head = await conn.scalar(
        select(func.max(operations.c.narrative_turn)).where(
            operations.c.story_id == story_id,
            operations.c.kind == "turn",
            operations.c.membership == "active",
        )
    )
    head_ops = select(operations.c.operation_id).where(
        operations.c.story_id == story_id,
        operations.c.narrative_turn == head,
        operations.c.membership == "active",
    )
    rows = await conn.execute(
        select(t.c.id)
        .where(
            t.c.story_id == story_id,
            or_(lifecycle.open_clause(t), t.c.resolution_operation_id.in_(head_ops)),
            t.c.operation_id.not_in(head_ops),
        )
        .order_by(t.c.narrative_turn.desc().nulls_last(), t.c.id.desc())
    )
    for (thunk_id,) in rows:
        thunk = await lifecycle.read_thunk(conn, story_id, str(thunk_id))
        if thunk is not None and thunk.open_slot in (OpenSlot.SUBJECT, OpenSlot.OBJECT):
            return thunk
    return None


def _filled(thunk: lifecycle.Thunk, entity_id: str) -> Proposition:
    sketch = thunk.sketch
    from epistrel.core.propositions.model import Interval

    return Proposition(
        subject_id=entity_id if sketch.open_slot is OpenSlot.SUBJECT else str(sketch.subject_id),
        predicate_id=sketch.predicate_id,
        object_id=entity_id if sketch.open_slot is OpenSlot.OBJECT else sketch.object_id,
        polarity=sketch.polarity,
        valid_time=Interval.of(None, None),
    )


def decision(thunk: lifecycle.Thunk, name: str) -> str:
    """The "Author decision" line: the thunk's question answered with ``name``."""
    sketch = thunk.sketch
    if sketch.open_slot is OpenSlot.SUBJECT:
        said = " ".join(x for x in (name, sketch.relation_text, sketch.object_text or "") if x)
    else:
        said = " ".join(x for x in (sketch.subject_text or "", sketch.relation_text, name) if x)
    return f"Author decision: {said.strip()}. Render it so; do not name anyone else."


async def prepare(db: AsyncEngine, models: ModelAccessLayer, story_id: str, text: str) -> Directed:
    """Parse the direction and reconcile its binding (see the module docstring)."""
    from epistrel.orchestrator import directives
    from epistrel.orchestrator.rewrite import parse as parsing
    from epistrel.orchestrator.rewrite.session import _registry

    async with db.connect() as conn:
        story = await parsing.load_story(
            conn, story_id, await _registry(conn, story_id), margin=0.15
        )
    parsed = await directives._parse(models, story, text)
    steer = f"Author direction (this reply only): {parsed.steer_text or text}"
    if not parsed.binding:
        return Directed(text, steer)
    async with db.begin() as conn:
        thunk = await _thunk(conn, story_id)
        if thunk is None:
            return Directed(text, steer, warnings=(BINDING_UNMATCHED,))
        name = parsed.binding[0].entity
        entity_id = await _entity(conn, story_id, name)
        binding = lifecycle.binding_of(thunk, _filled(thunk, entity_id))
        reconciled = await reconcile(conn, story_id, thunk, binding)
    log.info(
        "directed regeneration %s: thunk %s %s",
        story_id,
        thunk.id,
        "conflict" if isinstance(reconciled, Conflict) else "ok",
    )
    return Directed(
        text,
        decision(thunk, name),
        thunk.id,
        entity_id,
        binding,
        reconciled,
    )


async def head_variant(conn: AsyncConnection, story_id: str) -> dict[str, int]:
    """The active head turn and its variant (the variant a held regeneration leaves active)."""
    o = operations.c
    row = (
        await conn.execute(
            select(o.narrative_turn, o.turn_variant)
            .where(o.story_id == story_id, o.kind == "turn", o.membership == "active")
            .order_by(o.narrative_turn.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return {"turn": 0, "variant": 0}
    return {"turn": int(row.narrative_turn or 0), "variant": int(row.turn_variant or 0)}


async def compensate(db: AsyncEngine, story_id: str, amendment_id: str, *, principal: str) -> None:
    """Revert a directed resolution's amendment whose regeneration failed: an ``undo``
    amendment reverting it, in its own operation (the two stand or fall together)."""
    from epistrel.core.amendments.store import AmendmentKind, record_amendment, revert_amendment
    from epistrel.core.enums import OperationKind
    from epistrel.core.events import Operation

    async with Operation(
        db,
        story_id=story_id,
        kind=OperationKind.REWRITE_SESSION,
        principal=principal,
        bumps_revision=True,
    ) as op:

        async def step(conn: AsyncConnection) -> None:
            made = await record_amendment(
                conn, op, kind=AmendmentKind.UNDO, parts=[], undoes=amendment_id
            )
            await revert_amendment(conn, op, amendment_id, by_amendment_id=made.amendment_id)

        op.add_commit_step(step)
        await op.commit()


async def record(db: AsyncEngine, story_id: str, directed: Directed, *, principal: str) -> str:
    """The ``directives`` row of a committed directed regeneration (disposition
    ``directed_regeneration``)."""
    from sqlalchemy import insert

    from epistrel.core.ids import new_ulid
    from epistrel.core.tables import directives
    from epistrel.orchestrator import directives as author_directives

    directive_id = new_ulid()
    async with db.begin() as conn:
        await conn.execute(
            insert(directives).values(
                id=directive_id,
                story_id=story_id,
                principal=principal,
                text=directed.text,
                kind="durable" if directed.binding is not None else "ephemeral",
                classified_by="model",
                disposition="directed_regeneration",
                temporal_scope="now",
                changes=[
                    {
                        "steer": directed.steer,
                        "thunk_id": directed.thunk_id,
                        "entity_id": directed.entity_id,
                    }
                ],
            )
        )
        await author_directives._index(conn, directive_id)
    return directive_id


class DirectedPending(BaseModel):
    """A directed regeneration whose binding conflicts with recorded history (REST 202): the
    ``directed_resolution`` session holding it, and the variant still active; nothing is
    committed until the session is applied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: Literal["awaiting_rewrite"] = "awaiting_rewrite"
    rewrite_session: RewriteSession
    previous_variant: dict[str, int]
    timeline_revision: int
    warnings: list[str] = Field(default_factory=list)
    replayed: bool = False


def conflict_document(directed: Directed, overrides: dict[str, Any]) -> dict[str, Any]:
    """What ``rewrite.session.open_directed`` stores: the direction, the binding's implied
    obligations, the lines and records it conflicts with and the regeneration's overrides."""
    conflict = directed.reconciled
    assert isinstance(conflict, Conflict)  # noqa: S101 - only a conflict is held
    return {
        **directed.document(),
        "obligations": [
            o.proposition.model_dump(mode="json")
            for o in conflict.obligations
            if o.kind == "implied"
        ],
        "lines": list(conflict.conflicting_lines),
        "records": list(conflict.conflicting_records),
        "regenerate": overrides,
    }


__all__ = [
    "BINDING_UNMATCHED",
    "DECISION",
    "Directed",
    "DirectedPending",
    "compensate",
    "conflict_document",
    "decision",
    "directing",
    "head_variant",
    "prepare",
    "record",
]
