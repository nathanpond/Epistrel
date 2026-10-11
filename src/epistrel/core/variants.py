"""Variant chains (FR-STORE-12,18,28; docs/02 §4.6; #66): the tree of a story's turn variants,
the chains it is made of, and the plan of a swap between them.

- **The tree.** Every committed ``turn`` operation is a node ``(turn, variant)``; its parent is
  ``(turn - 1, parent_variant)``, the active variant of the turn before it when it committed
  (``operations.parent_variant``, #66). Variants are small integers per turn index,
  ``max(existing) + 1``, so a turn regenerated or written over a rewound tail never reuses one.
  Exactly one variant per index is active and the active nodes form one chain (``Operation``'s
  ``_check_chain`` checks the parent pointers too).
- **Chains.** A chain is a run of nodes that continue one another with the same membership,
  identified by the ``{turn, variant}`` of its first (diverging) node. A chain starts at turn
  1, where its parent has more than one child, or where the membership changes (a rewound
  tail under its kept history); ``list_chains`` reports each as ``{turn, variant, membership,
  tip_turn, turn_count, membership_tx, parent}`` (``GET /stories/{id}/variants``).
- **Selecting a chain.** ``Tree.resolve`` takes a ``{turn, variant}`` to the tip it leads to:
  from the chain's first node, on through the one child that shares its membership; a node
  with several such children makes the selector ambiguous, 409 ``unknown_chain`` with the
  ``candidates`` it continues into; an unknown node is 409 ``unknown_chain`` too.
- **A swap's plan** (``plan_swap``): the path from the selected tip down to the active chain
  (the *branch* turn, 0 when the paths part at turn 1). Every active committed operation
  positioned after the branch goes ``dormant``; every non-active committed operation positioned
  on the path (the turns and the work hanging off them, e.g. a summary segment at its
  ``covers_to``) becomes ``active``. ``stories.next_turn_index`` becomes the tip + 1. A swap to
  the active chain changes nothing (``None``). Swapping back selects the chain just made dormant
  and restores the exact prior memberships.
- **Reading along a chain** (``chain_operations``): ``history?variant=turn:variant`` and #67's
  ``fork {variant_chain}`` read the active history up to the branch and the selected chain's
  operations above it (``fork_selection`` validates ``at_turn``).

The swap operation itself (one ``control`` operation, one ``swap`` event) is
``core.timeline.swap``, which shares #65's edit protocol.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import Membership, OperationKind, OperationStatus
from epistrel.core.errors import InvalidRequest, StoryNotFound, UnknownChain
from epistrel.core.events.types import Head, Swap
from epistrel.core.tables import operations, stories

Key = tuple[int, int]
"""A node: ``(turn, variant)``."""

CHAIN_PATTERN = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")


class ChainRef(BaseModel):
    """A chain selector: the ``{turn, variant}`` of the chain's first (diverging) turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: int = Field(ge=1)
    variant: int = Field(ge=1)

    @classmethod
    def parse(cls, raw: str, *, field: str = "query.variant") -> Self:
        """``"turn:variant"`` (the query-string form); 422 otherwise."""
        match = CHAIN_PATTERN.match(raw)
        if match is None or int(match.group(1)) < 1 or int(match.group(2)) < 1:
            raise InvalidRequest(
                "a variant chain is written turn:variant, e.g. 801:2", {"field": field}
            )
        return cls(turn=int(match.group(1)), variant=int(match.group(2)))

    @property
    def key(self) -> Key:
        return (self.turn, self.variant)

    def wire(self) -> dict[str, int]:
        return {"turn": self.turn, "variant": self.variant}


def _chain_ref(value: Any) -> Any:
    return ChainRef.parse(value, field="body") if isinstance(value, str) else value


ChainSelector = Annotated[ChainRef, BeforeValidator(_chain_ref)]
"""A ``ChainRef`` given as ``{turn, variant}`` or ``"turn:variant"``."""


class SwapRequest(BaseModel):
    """``POST /stories/{id}/swap``: make the chain ``to_variant_chain`` the active one."""

    model_config = ConfigDict(extra="forbid")

    to_variant_chain: ChainSelector = Field(
        description="The chain to activate: the `{turn, variant}` of its first turn."
    )


@dataclass(frozen=True, slots=True)
class Node:
    operation_id: str
    turn: int
    variant: int
    parent: int | None
    membership: Membership
    membership_tx: datetime | None

    @property
    def key(self) -> Key:
        return (self.turn, self.variant)

    @property
    def ref(self) -> ChainRef:
        return ChainRef(turn=self.turn, variant=self.variant)


@dataclass(frozen=True, slots=True)
class Chain:
    """One chain of the story (``GET /stories/{id}/variants``)."""

    turn: int
    variant: int
    membership: Membership
    tip_turn: int
    turn_count: int
    membership_tx: datetime | None
    parent: ChainRef | None
    """The chain this one diverges from (``None`` for a chain starting at turn 1)."""


@dataclass(frozen=True, slots=True)
class Path:
    """The way from the active chain to a selected tip: the branch turn (0 when the paths part
    at turn 1) and the nodes above it, ascending. Empty when the tip is active."""

    branch: int
    nodes: tuple[Node, ...]
    tip: Node

    @property
    def keys(self) -> list[Key]:
        return [n.key for n in self.nodes]


class Tree:
    """A story's variant tree, read in one query (see the module docstring)."""

    def __init__(self, nodes: Sequence[Node]) -> None:
        self.nodes: dict[Key, Node] = {n.key: n for n in nodes}
        self.children: dict[Key | None, list[Node]] = defaultdict(list)
        for node in sorted(nodes, key=lambda n: n.key):
            self.children[self._parent_key(node)].append(node)

    @staticmethod
    def _parent_key(node: Node) -> Key | None:
        if node.turn <= 1 or node.parent is None:
            return None
        return (node.turn - 1, node.parent)

    def parent(self, node: Node) -> Node | None:
        key = self._parent_key(node)
        return None if key is None else self.nodes.get(key)

    @property
    def active(self) -> list[Node]:
        return sorted(
            (n for n in self.nodes.values() if n.membership is Membership.ACTIVE),
            key=lambda n: n.turn,
        )

    def starts_chain(self, node: Node) -> bool:
        parent = self.parent(node)
        return (
            parent is None
            or len(self.children[parent.key]) > 1
            or parent.membership is not node.membership
        )

    def _chain_of(self, node: Node) -> Node:
        """The first node of the chain ``node`` is on."""
        while not self.starts_chain(node):
            parent = self.parent(node)
            assert parent is not None  # noqa: S101 - a parentless node starts its chain
            node = parent
        return node

    def _segment(self, start: Node) -> list[Node]:
        nodes = [start]
        while True:
            kids = self.children[nodes[-1].key]
            if len(kids) != 1 or kids[0].membership is not start.membership:
                return nodes
            nodes.append(kids[0])

    def chains(self) -> list[Chain]:
        found: list[Chain] = []
        for node in sorted(self.nodes.values(), key=lambda n: n.key):
            if not self.starts_chain(node):
                continue
            segment = self._segment(node)
            parent = self.parent(node)
            found.append(
                Chain(
                    turn=node.turn,
                    variant=node.variant,
                    membership=node.membership,
                    tip_turn=segment[-1].turn,
                    turn_count=len(segment),
                    membership_tx=node.membership_tx,
                    parent=None if parent is None else self._chain_of(parent).ref,
                )
            )
        return found

    def resolve(self, ref: ChainRef) -> Node:
        """The tip ``ref`` leads to; 409 ``unknown_chain`` when it names no variant or
        continues in more than one way."""
        start = self.nodes.get(ref.key)
        if start is None:
            raise UnknownChain(
                f"turn {ref.turn} has no variant {ref.variant}", {"variant_chain": ref.wire()}
            )
        tip = start
        while True:
            kids = [k for k in self.children[tip.key] if k.membership is start.membership]
            if not kids:
                return tip
            if len(kids) > 1:
                raise UnknownChain(
                    f"the chain {ref.turn}:{ref.variant} continues in more than one way at turn "
                    f"{kids[0].turn}; name one of the candidates",
                    {
                        "variant_chain": ref.wire(),
                        "candidates": [k.ref.wire() for k in kids],
                    },
                )
            tip = kids[0]

    def path(self, tip: Node) -> Path:
        """From the active chain up to ``tip`` (see ``Path``)."""
        nodes: list[Node] = []
        node: Node | None = tip
        while node is not None and node.membership is not Membership.ACTIVE:
            nodes.append(node)
            node = self.parent(node)
        if node is None and nodes and nodes[-1].turn != 1:
            raise UnknownChain(
                f"the chain of turn {tip.turn} variant {tip.variant} has no recorded parent",
                {"variant_chain": tip.ref.wire()},
            )
        return Path(branch=0 if node is None else node.turn, nodes=tuple(reversed(nodes)), tip=tip)

    def leaf_of(self, operation_ids: Sequence[str]) -> Node | None:
        """The highest turn node among ``operation_ids`` (a truncation's tail)."""
        named = set(operation_ids)
        found = [n for n in self.nodes.values() if n.operation_id in named]
        return max(found, key=lambda n: n.turn) if found else None


async def load_tree(conn: AsyncConnection, story_id: str) -> Tree:
    found = await conn.execute(
        select(
            operations.c.operation_id,
            operations.c.narrative_turn,
            operations.c.turn_variant,
            operations.c.parent_variant,
            operations.c.membership,
            operations.c.membership_tx,
        ).where(
            operations.c.story_id == story_id,
            operations.c.kind == OperationKind.TURN.value,
            operations.c.status == OperationStatus.COMMITTED.value,
        )
    )
    return Tree(
        [
            Node(
                operation_id=r.operation_id,
                turn=int(r.narrative_turn),
                variant=int(r.turn_variant),
                parent=None if r.parent_variant is None else int(r.parent_variant),
                membership=Membership(r.membership),
                membership_tx=r.membership_tx,
            )
            for r in found
        ]
    )


async def _require_story(conn: AsyncConnection, story_id: str) -> None:
    found = await conn.scalar(select(stories.c.id).where(stories.c.id == story_id))
    if found is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})


async def list_chains(db: AsyncEngine | AsyncConnection, story_id: str) -> list[Chain]:
    """Every chain of the story (``GET /stories/{id}/variants``)."""
    async with _connection(db) as conn:
        await _require_story(conn, story_id)
        return (await load_tree(conn, story_id)).chains()


# -- the swap plan ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SwapPlan:
    """What a swap writes: the ``swap`` event's payload, the chain it activates, and each
    activated operation's previous membership (for the membership hooks)."""

    payload: Swap
    active_chain: ChainRef
    previous: tuple[tuple[str, Membership], ...]


def _positioned_on(keys: Sequence[Key]) -> Any:
    return tuple_(operations.c.narrative_turn, operations.c.turn_variant).in_(list(keys))


async def path_operations(
    conn: AsyncConnection, story_id: str, path: Path
) -> list[tuple[str, Membership]]:
    """The committed operations positioned on ``path``'s nodes, with their membership."""
    if not path.nodes:
        return []
    found = await conn.execute(
        select(operations.c.operation_id, operations.c.membership)
        .where(
            operations.c.story_id == story_id,
            operations.c.status == OperationStatus.COMMITTED.value,
            _positioned_on(path.keys),
        )
        .order_by(operations.c.narrative_turn, operations.c.operation_id)
    )
    return [(r.operation_id, Membership(r.membership)) for r in found]


async def plan_path_swap(conn: AsyncConnection, story_id: str, tree: Tree, path: Path) -> SwapPlan:
    """The swap that makes ``path`` active (``path.nodes`` must not be empty)."""
    outgoing = await conn.scalars(
        select(operations.c.operation_id)
        .where(
            operations.c.story_id == story_id,
            operations.c.status == OperationStatus.COMMITTED.value,
            operations.c.membership == Membership.ACTIVE.value,
            operations.c.narrative_turn > path.branch,
        )
        .order_by(operations.c.narrative_turn, operations.c.operation_id)
    )
    incoming = [
        (op, m)
        for op, m in await path_operations(conn, story_id, path)
        if m is not Membership.ACTIVE
    ]
    variants = {str(n.turn): n.variant for n in tree.active if n.turn <= path.branch}
    variants.update({str(n.turn): n.variant for n in path.nodes})
    ordered = dict(sorted(variants.items(), key=lambda item: int(item[0])))
    return SwapPlan(
        payload=Swap(
            turn=path.branch + 1,
            activated_operation_ids=[op for op, _ in incoming],
            dormant_operation_ids=list(outgoing),
            active_head=Head(next_turn_index=path.tip.turn + 1, active_variants=ordered),
        ),
        active_chain=path.nodes[0].ref,
        previous=tuple(incoming),
    )


async def plan_swap(conn: AsyncConnection, story_id: str, target: ChainRef) -> SwapPlan | None:
    """The swap to ``target``'s chain; ``None`` when it is already the active one."""
    await _require_story(conn, story_id)
    tree = await load_tree(conn, story_id)
    path = tree.path(tree.resolve(target))
    if not path.nodes:
        return None
    return await plan_path_swap(conn, story_id, tree, path)


# -- reading along a chain --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainSelection:
    """A chain to read along: the active history through ``branch``, then ``operation_ids``
    (every committed operation on the chain's nodes above it) through ``tip_turn``."""

    ref: ChainRef
    branch: int
    tip_turn: int
    operation_ids: tuple[str, ...]
    active: bool
    """``True`` when the chain is the active one (a read along it is the corrected history)."""

    def visible(self, membership: Any, turn: Any, operation_id: Any) -> Any:
        """SQL: a row on this chain, given its membership, turn and operation columns."""
        if self.active or not self.operation_ids:
            return membership == Membership.ACTIVE.value
        return or_(
            and_(membership == Membership.ACTIVE.value, turn <= self.branch),
            operation_id.in_(list(self.operation_ids)),
        )


async def chain_operations(conn: AsyncConnection, story_id: str, ref: ChainRef) -> ChainSelection:
    """Select ``ref``'s chain for a read (409 ``unknown_chain`` as ``Tree.resolve``)."""
    tree = await load_tree(conn, story_id)
    tip = tree.resolve(ref)
    path = tree.path(tip)
    found = await path_operations(conn, story_id, path)
    return ChainSelection(
        ref=ref,
        branch=path.branch if path.nodes else tip.turn,
        tip_turn=tip.turn,
        operation_ids=tuple(op for op, _ in found),
        active=not path.nodes,
    )


async def fork_selection(
    db: AsyncEngine | AsyncConnection, story_id: str, ref: ChainRef | None, at_turn: int | None
) -> tuple[ChainSelection | None, int]:
    """#67's ``fork {at_turn, variant_chain}`` selector: the chain to copy (``None``: the
    active one) and the turn the copy runs through (default the chain's tip). ``at_turn``
    above the chain's tip, or below its divergence, is 422."""
    async with _connection(db) as conn:
        await _require_story(conn, story_id)
        if ref is None:
            head = await conn.scalar(
                select(stories.c.next_turn_index).where(stories.c.id == story_id)
            )
            through = int(head or 1) - 1 if at_turn is None else at_turn
            return None, through
        selection = await chain_operations(conn, story_id, ref)
    through = selection.tip_turn if at_turn is None else at_turn
    if through > selection.tip_turn or (not selection.active and through < ref.turn):
        raise InvalidRequest(
            f"at_turn must be on the chain {ref.turn}:{ref.variant}, from turn {ref.turn} to "
            f"its tip {selection.tip_turn}",
            {"field": "body.at_turn", "from_turn": ref.turn, "tip_turn": selection.tip_turn},
        )
    return selection, through


@asynccontextmanager
async def _connection(db: AsyncEngine | AsyncConnection) -> AsyncIterator[AsyncConnection]:
    if isinstance(db, AsyncConnection):
        yield db
        return
    async with db.connect() as conn:
        yield conn


__all__ = [
    "Chain",
    "ChainRef",
    "ChainSelection",
    "ChainSelector",
    "Node",
    "Path",
    "SwapPlan",
    "SwapRequest",
    "Tree",
    "chain_operations",
    "fork_selection",
    "list_chains",
    "load_tree",
    "path_operations",
    "plan_path_swap",
    "plan_swap",
]
