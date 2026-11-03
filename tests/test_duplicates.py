"""#481's duplicate-candidate rule (pure; ``core.verify.duplicates``): a new place minted while
the scene's location is untracked or generic, and a new place, object or event whose descriptors
overlap an existing entity's of the same kind, is a notice naming both ids; a distinct new place
in a named scene is not."""

from __future__ import annotations

from epistrel.core.verify.duplicates import Known, candidates


def _reasons(found: list) -> list[tuple[str, str | None, str]]:  # type: ignore[type-arg]
    return [(c.new_id, c.existing_id, c.reason) for c in found]


def test_duplicate_candidates() -> None:
    inn = Known("N1", "place", "Crossroads Inn")
    tavern = Known("T1", "place", "the tavern")
    room = Known("R1", "place", "the room")
    hall = Known("H1", "place", "the great hall")
    # Descriptor overlap: "the inn" beside "Crossroads Inn".
    new_inn = Known("N2", "place", "the inn")
    assert _reasons(candidates([new_inn], [inn, new_inn], hall)) == [
        ("N2", "N1", "descriptor_overlap")
    ]
    # An untracked scene location: the new place may be it.
    assert _reasons(candidates([tavern], [tavern], None)) == [
        ("T1", None, "untracked_scene_location")
    ]
    # A generic scene location ("the room"): the new place may be where the scene is.
    assert _reasons(candidates([tavern], [tavern, room], room)) == [
        ("T1", "R1", "generic_scene_location")
    ]
    # Events too: "the errand" after "the bank errand".
    errand = Known("E1", "event", "the bank errand")
    later = Known("E2", "event", "the errand")
    assert _reasons(candidates([later], [errand, later], hall)) == [
        ("E2", "E1", "descriptor_overlap")
    ]
    # Negative: a distinct new place in a named scene; another kind never pairs.
    mill = Known("M1", "place", "the old mill")
    key = Known("K1", "item", "the inn key")
    assert candidates([mill], [inn, mill, key], hall) == []
    assert _reasons(candidates([key], [inn, key], hall)) == []
