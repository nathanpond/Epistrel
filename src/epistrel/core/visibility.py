"""The one visibility shape every fact, memory and belief carries (FR-EPI-5; docs/02 §4.4; #133
created it, #159 shares it).

A record's ``visibility`` is ``{"scope": objective|public|engine|characters, "characters":
[...]}``: ``objective`` (canon narration) and ``public`` (publicly known) reach every viewpoint,
``engine`` (true ages) only ``omniscient``, and ``characters`` only the characters it lists and
``omniscient``. ``characters`` is non-empty exactly when the scope is ``characters``: the model
validates it, and every table that stores the shape has the CHECK ``VISIBILITY_CHARACTERS_CHECK``
(``facts``, ``memories``, ``beliefs``). A belief is private by construction: its visibility is
``private_to(holder)`` and nothing else (``beliefs``' own CHECK).

``narrator``, ``director`` and ``player`` readers (``READER_VIEWPOINTS``) are no character: they
see ``objective`` and ``public`` only. The scope alone decides; a record's holder does not
(ADR-167): a per-perceiver memory is written ``characters: [perceiver]``.

``visible_clause`` is the predicate in SQL and ``visible_to`` the same test in Python.
``core.canon.facts`` re-exports all of it (``core.verify.neighborhood``, ``core.verify.protected``
and ``core.projections.structured`` import it from there). Retrieval composes its viewpoint filter
from these (``core.retrieval.viewpoint``)."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import ColumnElement, and_, or_

from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT

VisibilityScope = Literal["objective", "public", "engine", "characters"]

VISIBILITY_SCOPES: tuple[str, ...] = ("objective", "public", "engine", "characters")
"""The scopes a record may carry (``visibility->>'scope'``, CHECK)."""

READER_VIEWPOINTS: frozenset[str] = frozenset({"narrator", "director", "player"})
"""Readers that are no character: they see ``objective`` and ``public`` records only."""

SHARED_SCOPES: tuple[str, ...] = ("objective", "public")
"""The scopes every viewpoint sees."""

VISIBILITY_SCOPE_CHECK = "visibility->>'scope' IN ('objective', 'public', 'engine', 'characters')"
"""The scope CHECK every table storing the shape has."""

VISIBILITY_CHARACTERS_CHECK = (
    "(visibility->>'scope' = 'characters') = "
    "(jsonb_typeof(visibility->'characters') = 'array' "
    "AND jsonb_array_length(visibility->'characters') > 0)"
)
"""The CHECK every table storing the shape has: ``characters`` is a non-empty array exactly when
the scope is ``characters``."""


class Visibility(BaseModel):
    """Who may read a record: ``objective`` (canon narration), ``public`` (publicly known),
    ``engine`` (engine-only, e.g. true ages: ``omniscient`` alone) or ``characters`` (only the
    characters listed, e.g. a ``latent`` beat's scope or a belief's holder, and ``omniscient``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: VisibilityScope = "objective"
    characters: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _characters(self) -> Self:
        if self.scope == "characters":
            if not self.characters:
                raise ValueError("a characters scope names at least one character")
            object.__setattr__(self, "characters", tuple(sorted(set(self.characters))))
        elif self.characters is not None:
            raise ValueError(f"a {self.scope} scope names no characters")
        return self

    def document(self) -> dict[str, Any]:
        """The stored ``visibility`` document."""
        if self.scope == "characters":
            return {"scope": self.scope, "characters": list(self.characters or ())}
        return {"scope": self.scope}


OBJECTIVE = Visibility()


def private_to(character_id: str) -> Visibility:
    """The visibility of what one character alone may read: a belief of its holder (#159), a
    per-perceiver memory (#161)."""
    return Visibility(scope="characters", characters=(character_id,))


def visible_clause(table: Any, viewpoint: str) -> ColumnElement[bool] | None:
    """The visibility predicate on ``table`` (any table or alias with a ``visibility`` column)
    for ``viewpoint`` (``None``: everything, the ``omniscient`` read)."""
    if viewpoint == OMNISCIENT:
        return None
    scope = table.c.visibility["scope"].astext
    shared: ColumnElement[bool] = scope.in_(SHARED_SCOPES)
    if not viewpoint.startswith(CHARACTER_VIEWPOINT_PREFIX):
        return shared
    member = viewpoint.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
    return or_(
        shared,
        and_(scope == "characters", table.c.visibility.contains({"characters": [member]})),
    )


def visible_to(visibility: Visibility, viewpoint: str) -> bool:
    """``visible_clause`` for one record in Python."""
    if viewpoint == OMNISCIENT:
        return True
    if visibility.scope in SHARED_SCOPES:
        return True
    if visibility.scope != "characters" or not viewpoint.startswith(CHARACTER_VIEWPOINT_PREFIX):
        return False
    return viewpoint.removeprefix(CHARACTER_VIEWPOINT_PREFIX) in (visibility.characters or ())


__all__ = [
    "OBJECTIVE",
    "READER_VIEWPOINTS",
    "SHARED_SCOPES",
    "VISIBILITY_CHARACTERS_CHECK",
    "VISIBILITY_SCOPES",
    "VISIBILITY_SCOPE_CHECK",
    "Visibility",
    "VisibilityScope",
    "private_to",
    "visible_clause",
    "visible_to",
]
