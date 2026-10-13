"""The document check (#111): what is validated is a valid draft of its kind and, for a scenario,
keeps every save rule (``core.catalog.scenario_rules``).

A save validates every draft it stores, so a stored draft fails here only when a rule changed
since it was saved. Each failure is an error at its field: the code is the schema error's type
(``string_type``, ``extra_forbidden``) or the save rule's (``unknown_role``,
``role_id_duplicate``), and the rule is its message. The check gates: the other checks read a
valid document, so none runs after it fails.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from pydantic import ValidationError

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.report import Entry, Outcome
from epistrel.core.catalog.scenario_models import ScenarioDraft
from epistrel.core.catalog.scenario_rules import dotted, rule_errors
from epistrel.core.tables.ownership import EntityType


def _role_at(document: Mapping[str, Any], loc: Sequence[str | int]) -> str | None:
    """The id of the role ``loc`` points into (``cast.roles[i]…``), when the document names it."""
    if len(loc) < 3 or tuple(loc[:2]) != ("cast", "roles") or not isinstance(loc[2], int):
        return None
    cast = document.get("cast")
    roles = cast.get("roles") if isinstance(cast, Mapping) else None
    if not isinstance(roles, list) or not 0 <= loc[2] < len(roles):
        return None
    role = roles[loc[2]]
    found = role.get("id") if isinstance(role, Mapping) else None
    return found if isinstance(found, str) else None


def _entries(subject: Subject, errors: Sequence[Mapping[str, Any]]) -> list[Entry]:
    return [
        subject.entry(
            str(error["type"]),
            Outcome.rejected,
            field=dotted(tuple(error.get("loc", ()))),
            role=_role_at(subject.document, tuple(error.get("loc", ()))),
            rule=str(error["msg"]),
        )
        for error in errors
    ]


class DocumentCheck(Check):
    """See the module docstring."""

    name = "document"
    kinds: ClassVar[frozenset[EntityType]] = frozenset({EntityType.SCENARIO, EntityType.CHARACTER})
    gate = True

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        try:
            parsed = subject.model.model_validate(subject.document)
        except ValidationError as exc:
            return Findings(entries=_entries(subject, list(exc.errors())))
        if isinstance(parsed, ScenarioDraft):
            return Findings(entries=_entries(subject, rule_errors(parsed)))
        return Findings()


CHECK = DocumentCheck()

__all__ = ["CHECK", "DocumentCheck"]
