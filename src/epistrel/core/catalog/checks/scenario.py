"""Helpers the scenario checks share (#111): the parsed document of a ``Subject`` and where a role
or a seed sits in it. Not a check."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from epistrel.core.catalog.checks.base import Subject
from epistrel.core.catalog.scenario_models import RoleCharacter, ScenarioDraft, ScenarioRole, Seed
from epistrel.core.catalog.scenario_rules import dotted

DRAFT_MEMO = "scenario_draft"


def draft_of(subject: Subject) -> ScenarioDraft:
    """The subject's document as a ``ScenarioDraft``, parsed once per validation (the document
    check runs first and gates the others, so the document is valid here)."""
    found = subject.memo.get(DRAFT_MEMO)
    if not isinstance(found, ScenarioDraft):
        found = ScenarioDraft.model_validate(subject.document)
        subject.memo[DRAFT_MEMO] = found
    return found


def role_field(index: int, *path: str | int) -> str:
    """The field path of ``path`` inside the ``index``-th role: ``cast.roles[2].character``."""
    return dotted(("cast", "roles", index, *path))


@dataclass(frozen=True, slots=True)
class SeedAt:
    """A seed and where it sits: its field path, and the role whose list holds it (``None`` for
    the world's seeds and a planned reveal's truth)."""

    field: str
    role: str | None
    seed: Seed


def seeds_at(draft: ScenarioDraft) -> Iterator[SeedAt]:
    """Every seed of ``draft`` (``ScenarioDraft.seeds``' order) with its place."""
    for loc, seed in draft.seeds():
        role: str | None = None
        if loc[:2] == ("cast", "roles") and isinstance(loc[2], int):
            role = draft.cast.roles[loc[2]].id
        yield SeedAt(field=dotted(loc), role=role, seed=seed)


def filled_roles(
    draft: ScenarioDraft,
) -> Iterator[tuple[int, ScenarioRole, RoleCharacter]]:
    """Each ``locked`` or ``default`` role that names its catalog character: its index, the
    role, and the character it names."""
    for index, role in enumerate(draft.cast.roles):
        if role.fill in ("locked", "default") and role.character is not None:
            yield index, role, role.character


__all__ = ["SeedAt", "draft_of", "filled_roles", "role_field", "seeds_at"]
