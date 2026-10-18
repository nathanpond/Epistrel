"""Canonization: a permitted ``novel`` claim becomes canon with its origin recorded
(FR-VERIFY-4,11; docs/02 §4.9; #144).

``canonize(conn, story_id, claim, provenance=..., operation=...)`` is #133's ``commit_fact``
inside the writing ``Operation``'s commit step (so the fact commits with the turn that
asserted it, under the story lock, through the canon store's own erasure guard and
``fact_committed`` event), with:

- **provenance** from the asserter: ``render``, ``director``, ``player`` or ``author``
  (``core.verify.permission.Asserter.provenance``; #133's shared enum); the authority follows
  it (``default_authority``);
- **visibility** from the beat (``visibility_of``): ``objective`` by default, a ``latent``
  beat's declared scope (the characters it names);
- an optional **anchor** (``anchored_to``): a related fact, given or chosen from the claim's
  neighborhood by ``anchor_for``: the neighborhood's fact with the same subject and predicate,
  else the most recent fact about the same subject (none: no anchor);
- the beat's narrative position (``beat``; the operation's turn) and ``commit_state =
  committed``;
- **new entities on first canonized mention** (``introduces``): a place or object the claim
  names that no entity holds yet carries a fresh id (minted by the caller, #150's detection)
  and its name; it becomes an entity (``propositions.store.add_entity``, with that id) only
  when the claim is canonized, its ``kind`` from the predicate's argument type on that side
  (``kind_for``; ``other`` when the type is no entity kind). A denied claim creates nothing.

**Only permitted claims.** ``CanonRequest`` is what the verifier hands over for each permitted
novel claim (``orchestrator.verify.verifier.Verifier.canon_requests``); a denied claim never
has one (FR-VERIFY-12: it goes to #151's repair). ``canonize_all`` writes a turn's requests in
order and dedupes identical claims (same proposition, provenance and visibility) to one write;
the first keeps its beat. Nothing here decides permission or reads canon.

Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon.facts import OBJECTIVE, Fact, FactOperation, Visibility, commit_fact
from epistrel.core.enums import FactCommitState, RecordProvenance
from epistrel.core.propositions import store
from epistrel.core.propositions.model import ENTITY_KINDS, Instant, Predicate, Proposition
from epistrel.core.propositions.registry import Registry, load

log = logging.getLogger(__name__)

CANONIZING_PROVENANCES: frozenset[RecordProvenance] = frozenset(
    {
        RecordProvenance.RENDER,
        RecordProvenance.DIRECTOR,
        RecordProvenance.PLAYER,
        RecordProvenance.AUTHOR,
    }
)
"""The provenances a canonized claim records (FR-VERIFY-4)."""

LATENT = "latent"
OTHER = "other"


@dataclass(frozen=True, slots=True)
class NewEntity:
    """A place or object a canonized claim names for the first time: the id the claim uses
    for it and its name."""

    entity_id: str
    name: str


def kind_for(claim: Proposition, entity_id: str, predicate: Predicate | None) -> str:
    """The entity kind of ``entity_id`` in ``claim``: the predicate's subject type when it is
    the subject, else its object type; ``other`` when that type is no entity kind."""
    if predicate is None:
        return OTHER
    side = predicate.subject_type if claim.subject_id == entity_id else predicate.object_type
    return side if side in ENTITY_KINDS else OTHER


def visibility_of(beat_kind: str | None, scope: Sequence[str] | None = None) -> Visibility:
    """A canonized claim's visibility from its beat: a ``latent`` beat's declared scope (the
    characters it names), else ``objective``."""
    if beat_kind == LATENT and scope:
        return Visibility(scope="characters", characters=tuple(scope))
    return OBJECTIVE


def _start(claim: Proposition) -> int:
    vt = claim.valid_time
    if isinstance(vt, Instant):
        return vt.at
    return vt.start.outer if vt.start is not None else -(2**62)


def anchor_for(
    claim: Proposition, neighborhood: Iterable[Fact | tuple[str, Proposition]]
) -> str | None:
    """The fact a canonized ``claim`` is anchored to, among its neighborhood (facts, or
    ``(fact id, proposition)`` pairs): the one with the same subject and predicate (the most
    recent by valid-time, then id), else the most recent about the same subject; ``None`` when
    the neighborhood has neither."""
    pairs = [(f.id, f.proposition) if isinstance(f, Fact) else f for f in neighborhood]
    same_subject = [(i, p) for i, p in pairs if p.subject_id == claim.subject_id]
    same_key = [(i, p) for i, p in same_subject if p.predicate_id == claim.predicate_id]
    chosen = same_key or same_subject
    if not chosen:
        return None
    return max(chosen, key=lambda pair: (_start(pair[1]), pair[0]))[0]


async def canonize(
    conn: AsyncConnection,
    story_id: str,
    claim: Proposition,
    *,
    provenance: RecordProvenance,
    operation: FactOperation,
    visibility: Visibility = OBJECTIVE,
    anchored_to: str | None = None,
    neighborhood: Sequence[Fact] = (),
    beat: int | None = None,
    source_event_ids: Sequence[str] = (),
    introduces: Sequence[NewEntity] = (),
    registry: Registry | None = None,
) -> Fact:
    """Write the permitted novel ``claim`` as canon in ``operation``'s commit transaction (see
    the module docstring); answers the fact version. ``anchored_to`` wins over the anchor
    ``anchor_for`` would choose from ``neighborhood``; ``introduces`` are created first."""
    chosen = RecordProvenance(provenance)
    if chosen not in CANONIZING_PROVENANCES:
        raise ValueError(f"a canonized claim's provenance is not {chosen.value!r}")
    if introduces:
        registry = registry or await load(conn, story_id)
        predicate = registry.get(claim.predicate_id)
        for new in introduces:
            await store.add_entity(
                conn,
                story_id,
                kind_for(claim, new.entity_id, predicate),
                new.name,
                entity_id=new.entity_id,
                operation_id=operation.operation_id,
            )
    anchor = anchored_to if anchored_to is not None else anchor_for(claim, neighborhood)
    return await commit_fact(
        conn,
        story_id,
        claim,
        provenance=chosen,
        visibility=visibility,
        operation=operation,
        anchored_to=anchor,
        beat=beat,
        source_event_ids=source_event_ids,
        commit_state=FactCommitState.COMMITTED,
        registry=registry,
    )


@dataclass(frozen=True, slots=True)
class CanonRequest:
    """One permitted novel claim to canonize at the turn's commit."""

    claim: Proposition
    provenance: RecordProvenance
    visibility: Visibility = OBJECTIVE
    anchored_to: str | None = None
    beat: int | None = None
    claim_id: str | None = None
    introduces: tuple[NewEntity, ...] = ()

    def identity(self) -> tuple[str, str, str]:
        """What makes two requests the same write: the proposition, provenance and
        visibility (the first beat that asserted it keeps its position)."""
        return (
            self.claim.model_dump_json(),
            self.provenance.value,
            self.visibility.model_dump_json(),
        )


async def canonize_all(
    conn: AsyncConnection,
    story_id: str,
    requests: Sequence[CanonRequest],
    *,
    operation: FactOperation,
    registry: Registry | None = None,
) -> list[Fact]:
    """Canonize a turn's ``requests`` in order (a commit step), identical claims once and each
    new entity at its first mention."""
    seen: set[tuple[str, str, str]] = set()
    created: set[str] = set()
    written: list[Fact] = []
    for request in requests:
        identity = request.identity()
        if identity in seen:
            continue
        seen.add(identity)
        introduces = tuple(n for n in request.introduces if n.entity_id not in created)
        created.update(n.entity_id for n in introduces)
        written.append(
            await canonize(
                conn,
                story_id,
                request.claim,
                provenance=request.provenance,
                operation=operation,
                visibility=request.visibility,
                anchored_to=request.anchored_to,
                beat=request.beat,
                introduces=introduces,
                registry=registry,
            )
        )
    log.debug("canonized %d of %d permitted claims", len(written), len(requests))
    return written


__all__ = [
    "CANONIZING_PROVENANCES",
    "CanonRequest",
    "NewEntity",
    "anchor_for",
    "canonize",
    "canonize_all",
    "kind_for",
    "visibility_of",
]
