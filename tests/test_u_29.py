"""U-29: rating inheritance and change classification (docs/04 §3).

- M2 (#48): the effective-guardrail closure and the cycle check (pure), and the guidance a story
  resolves.
- M3 (#111): ``test_u_29__mismatch``, looser-than (FR-RATE-5), pure over an in-memory graph.
  Every U-29 (M3) part lives in this file: #118's ``test_u_29__classification`` (FR-RATE-6,7)
  classifies a rating change the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.catalog.checks.rating_mismatch import is_looser
from epistrel.core.ratings import (
    Guardrail,
    RatingNode,
    effective_guardrails,
    find_cycle,
    invalidate_closure_cache,
    story_guidance,
)
from epistrel.core.ratings.change import Direction, classify
from epistrel.core.seeds.ratings import (
    EVERYONE_ID,
    EXPLICIT_ID,
    MATURE_ID,
    SEED_RATINGS,
    TEEN_ID,
)
from epistrel.core.stories import create_story

OWN = {EVERYONE_ID: "p-everyone", TEEN_ID: "p-teen", MATURE_ID: "p-mature", EXPLICIT_ID: "p-x"}


def _seeded(policies: Mapping[str, tuple[str, ...]] | None = None) -> dict[str, RatingNode]:
    """The seeded graph, each rating given ``policies[id]`` (default: one policy of its own)."""
    policies = policies or {rid: (pid,) for rid, pid in OWN.items()}
    return {
        seed.id: RatingNode(
            id=seed.id,
            version_id=seed.version_id,
            name=seed.name,
            display_order=seed.display_order,
            policy_ids=policies.get(seed.id, ()),
            inherits_from=seed.inherits_from,
        )
        for seed in SEED_RATINGS
    }


def _node(rid: str, order: int, policies: tuple[str, ...], parents: tuple[str, ...]) -> RatingNode:
    return RatingNode(
        id=rid,
        version_id=f"v-{rid}",
        name=rid,
        display_order=order,
        policy_ids=policies,
        inherits_from=parents,
    )


@pytest.mark.part("M2", "FR-RATE-1,2")
def test_u_29() -> None:
    """With the seeded chain, Everyone's effective guardrails are its own plus Teen's, Mature's
    and Explicit's, each tagged with its source, in source display order; complement: Teen's
    set does not include Everyone's own policy. A policy reached by two paths appears once; an
    inheritance cycle is found on save (complement: an acyclic save finds none)."""
    # The closure cache is keyed by rating and version ids, and this graph reuses the seeded
    # ids: a closure computed from the real seeded rows earlier in the session would answer.
    invalidate_closure_cache()
    graph = _seeded()
    assert effective_guardrails(EVERYONE_ID, graph) == (
        Guardrail("p-everyone", EVERYONE_ID),
        Guardrail("p-teen", TEEN_ID),
        Guardrail("p-mature", MATURE_ID),
        Guardrail("p-x", EXPLICIT_ID),
    )
    teen = effective_guardrails(TEEN_ID, graph)
    assert [g.policy_id for g in teen] == ["p-teen", "p-mature", "p-x"]
    assert effective_guardrails(EXPLICIT_ID, graph) == (Guardrail("p-x", EXPLICIT_ID),)

    # Two paths: D inherits B and C, which both inherit A; "shared" is A's and appears once.
    diamond = {
        "A": _node("A", 40, ("shared", "a-only"), ()),
        "B": _node("B", 20, ("b-only",), ("A",)),
        "C": _node("C", 30, ("c-only",), ("A",)),
        "D": _node("D", 10, (), ("B", "C")),
    }
    found = effective_guardrails("D", diamond)
    assert [g.policy_id for g in found].count("shared") == 1
    assert found == (
        Guardrail("b-only", "B"),
        Guardrail("c-only", "C"),
        Guardrail("a-only", "A"),
        Guardrail("shared", "A"),
    )
    # The same policy listed by B and by A: once, from the nearer rating. (A new B version: the
    # closure cache is keyed by version ids, and a version's content never changes.)
    overlap = {**diamond, "B": replace(_node("B", 20, ("shared",), ("A",)), version_id="v-B2")}
    assert [g for g in effective_guardrails("D", overlap) if g.policy_id == "shared"] == [
        Guardrail("shared", "B")
    ]

    # Explicit inheriting from Everyone closes the chain into a loop.
    assert find_cycle(EXPLICIT_ID, [EVERYONE_ID], graph) == [
        EXPLICIT_ID,
        EVERYONE_ID,
        TEEN_ID,
        MATURE_ID,
        EXPLICIT_ID,
    ]
    assert find_cycle(EVERYONE_ID, [EVERYONE_ID], graph) == [EVERYONE_ID, EVERYONE_ID]
    assert find_cycle(EXPLICIT_ID, [], graph) is None
    assert find_cycle(EVERYONE_ID, [TEEN_ID, EXPLICIT_ID], graph) is None
    assert find_cycle("D", ["B", "C"], diamond) is None


@pytest.mark.integration
@pytest.mark.part("M2", "FR-RATE-2")
async def test_u_29__guidance(engine: AsyncEngine) -> None:
    """Prompt guidance is not inherited: a story at Everyone resolves Everyone's own guidance
    only, never Teen's (complement: a story at Teen resolves Teen's)."""
    seeds = {seed.id: seed for seed in SEED_RATINGS}
    everyone_story = await create_story(engine, principal="p-test")
    resolved = await story_guidance(engine, everyone_story)
    assert resolved.rating_id == EVERYONE_ID
    assert resolved.prompt_guidance == seeds[EVERYONE_ID].prompt_guidance
    assert seeds[TEEN_ID].prompt_guidance not in resolved.prompt_guidance
    assert resolved.image_guidance == seeds[EVERYONE_ID].image_guidance

    teen_story = await create_story(engine, principal="p-test", content_rating_id=TEEN_ID)
    assert (await story_guidance(engine, teen_story)).prompt_guidance == (
        seeds[TEEN_ID].prompt_guidance
    )


def test_closure_cache_is_keyed_by_versions() -> None:
    """The closure is cached per (rating, versions in the closure): an unchanged graph reuses
    the result, a new version of an inherited rating recomputes it, and invalidation clears."""
    invalidate_closure_cache()
    graph = _seeded()
    first = effective_guardrails(EVERYONE_ID, graph)
    assert effective_guardrails(EVERYONE_ID, dict(graph)) is first

    edited = dict(graph)
    old = graph[EXPLICIT_ID]
    edited[EXPLICIT_ID] = RatingNode(
        id=old.id,
        version_id="v-explicit-2",
        name=old.name,
        display_order=old.display_order,
        policy_ids=("p-x", "p-x2"),
        inherits_from=(),
    )
    assert [g.policy_id for g in effective_guardrails(EVERYONE_ID, edited)][-2:] == [
        "p-x",
        "p-x2",
    ]
    invalidate_closure_cache()
    again = effective_guardrails(EVERYONE_ID, graph)
    assert again == first
    assert again is not first


def test_closure_skips_unknown_ratings() -> None:
    """A rating missing from the graph contributes nothing (and an unknown root has none)."""
    graph = {"A": _node("A", 10, ("a",), ("gone",))}
    assert effective_guardrails("A", graph) == (Guardrail("a", "A"),)
    assert effective_guardrails("gone", graph) == ()
    assert find_cycle("A", ["gone"], graph) is None


def _guarded(rating_id: str, graph: Mapping[str, RatingNode]) -> set[str]:
    return {g.policy_id for g in effective_guardrails(rating_id, graph)}


@pytest.mark.part("M3", "FR-RATE-5")
def test_u_29__mismatch() -> None:
    """Looser-than compares guardrail sets, never display order. With the seeded chain and one
    policy of each rating's own, an Explicit item against an Everyone scenario is looser (it
    lacks Everyone's, Teen's and Mature's policies), and an Everyone item against an Explicit
    scenario is not; an item at the scenario's own rating is not looser. Two ratings that each
    list a policy the other lacks are each looser than the other, and swapping their display
    orders changes neither answer, nor does giving the looser rating the first place.

    Complement asserted: with no policy at all (the seeded ratings as shipped), nothing is
    looser in either direction."""
    invalidate_closure_cache()
    graph = _seeded()
    assert is_looser(_guarded(EXPLICIT_ID, graph), _guarded(EVERYONE_ID, graph))
    assert not is_looser(_guarded(EVERYONE_ID, graph), _guarded(EXPLICIT_ID, graph))
    assert not is_looser(_guarded(TEEN_ID, graph), _guarded(TEEN_ID, graph))
    assert is_looser(_guarded(MATURE_ID, graph), _guarded(TEEN_ID, graph))

    for first, second in ((10, 20), (20, 10)):
        invalidate_closure_cache()
        sideways = {
            "violence": _node("violence", first, ("p-violence",), ()),
            "romance": _node("romance", second, ("p-romance",), ()),
        }
        assert is_looser(_guarded("violence", sideways), _guarded("romance", sideways))
        assert is_looser(_guarded("romance", sideways), _guarded("violence", sideways))

    invalidate_closure_cache()
    reordered = {
        rid: replace(node, display_order=-node.display_order) for rid, node in graph.items()
    }
    assert is_looser(_guarded(EXPLICIT_ID, reordered), _guarded(EVERYONE_ID, reordered))
    assert not is_looser(_guarded(EVERYONE_ID, reordered), _guarded(EXPLICIT_ID, reordered))

    invalidate_closure_cache()
    bare = _seeded({rid: () for rid in OWN})
    assert not is_looser(_guarded(EXPLICIT_ID, bare), _guarded(EVERYONE_ID, bare))
    assert not is_looser(_guarded(EVERYONE_ID, bare), _guarded(EXPLICIT_ID, bare))
    invalidate_closure_cache()


def _ids(rating_id: str, graph: Mapping[str, RatingNode]) -> list[str]:
    return [g.policy_id for g in effective_guardrails(rating_id, graph)]


@pytest.mark.part("M3", "FR-RATE-6,7")
def test_u_29__classification() -> None:
    """A rating change is classified by guardrail sets, never by display order. With the
    seeded chain and one policy of each rating's own, Teen to Explicit is relaxing (Explicit's
    set is a subset of Teen's) and Explicit to Teen is tightening. Two custom ratings
    Mature-Violence and Mature-Romance, each with a policy the other lacks, are a sideways
    move: tightening in both directions, whatever their display order.

    Complement asserted: a move between ratings with equal sets (the seeded ratings as shipped,
    no policies) is relaxing both ways, and reversing every display order changes no
    answer."""
    invalidate_closure_cache()
    graph = _seeded()
    assert classify(_ids(TEEN_ID, graph), _ids(EXPLICIT_ID, graph)) is Direction.RELAX
    assert classify(_ids(EXPLICIT_ID, graph), _ids(TEEN_ID, graph)) is Direction.TIGHTEN
    assert classify(_ids(EVERYONE_ID, graph), _ids(MATURE_ID, graph)) is Direction.RELAX
    assert classify(_ids(MATURE_ID, graph), _ids(EVERYONE_ID, graph)) is Direction.TIGHTEN

    for first, second in ((10, 20), (20, 10)):
        invalidate_closure_cache()
        sideways = {
            **graph,
            "violence": _node("violence", first, ("p-violence",), (EXPLICIT_ID,)),
            "romance": _node("romance", second, ("p-romance",), (EXPLICIT_ID,)),
        }
        violence, romance = _ids("violence", sideways), _ids("romance", sideways)
        assert classify(violence, romance) is Direction.TIGHTEN
        assert classify(romance, violence) is Direction.TIGHTEN

    invalidate_closure_cache()
    reordered = {
        rid: replace(node, display_order=-node.display_order) for rid, node in graph.items()
    }
    assert classify(_ids(TEEN_ID, reordered), _ids(EXPLICIT_ID, reordered)) is Direction.RELAX
    assert classify(_ids(EXPLICIT_ID, reordered), _ids(TEEN_ID, reordered)) is Direction.TIGHTEN

    invalidate_closure_cache()
    bare = _seeded({rid: () for rid in OWN})
    assert classify(_ids(EXPLICIT_ID, bare), _ids(TEEN_ID, bare)) is Direction.RELAX
    assert classify(_ids(TEEN_ID, bare), _ids(EXPLICIT_ID, bare)) is Direction.RELAX
    invalidate_closure_cache()
