"""The one validation report shape (FR-SCN-5, ADR-141; #111).

Publishing a catalog entry, validating one on demand (``POST /catalog/scenarios/{id}/validate``)
and creating a story from a scenario (#119) answer the same model: a ``ValidationReport`` with
``errors`` and ``warnings``, each an ``Entry``, and ``unavailable``, the names of the checks the
deployment could not run (``check_unavailable``: listed, never silently passed).

An ``Entry`` has a stable ``code`` (an open string: ``character_missing``, ``rating_looser``,
``input_policy``, ``personality_in_role``, …), its ``location`` (the catalog ``entry``, the
``version`` validated or ``"draft"``, and where available the ``role``, the dotted ``field``
and the seed's server-assigned ``seed_id``), the exact ``text`` affected when there is one,
the ``outcome`` (what happened to the item), the ``source`` that prevailed or that the entry
names (``catalog_id@version`` of a character, a media asset's id, the character whose
personality applies), and the ``rule`` applied (``personality: character over scenario``, a
policy's id, or the rule a refused item breaks).

An entry is an error exactly when its outcome is ``rejected``; every other outcome is a warning.
Publishing fails on any error and succeeds with warnings. Entries are sorted by location, then
code (``sort_key``), so the same report always reads the same.

The INFO line a publish logs (``log_report``) holds codes and locations only, never ``text``
(invariant 3).
"""

from __future__ import annotations

import types
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.observability.logging import get_logger

log = get_logger(__name__)

DRAFT: Final = "draft"
"""``location.version`` of a draft (a publish validates the draft it publishes)."""

SPAN_MAX: Final = 200
"""The longest ``text`` an ``input_policy`` entry quotes: the matched span, or the start of the
text when no span can be located."""


CODES: Mapping[str, str] = types.MappingProxyType(
    {
        # errors (outcome ``rejected``)
        "field_required": "a field a published entry must hold is missing (a name; a catalog "
        "character's definition, FR-CAT-2)",
        "content_rating_missing": "a published scenario or catalog character has no content "
        "rating (FR-RATE-3)",
        "rating_retired": "the content rating is unknown or retired",
        "true_age_missing": "a published catalog character has no true age (FR-CAT-12)",
        "reference_image_missing_audience": "a catalog character's reference image declares no "
        "audience; there is no default (FR-CAT-4)",
        "primary_image_required": "the deployment requires a primary image to publish a catalog "
        "character (require_primary_image, FR-CAT-4)",
        "role_required": "a published scenario has no role",
        "world_required": "a published scenario has no world description",
        "opening_required": "a published scenario has no opening narration",
        "cast_limit_exceeded": "more roles than the starting-cast limit, or a limit above the "
        "ceiling (FR-SCN-4)",
        "character_missing": "a locked or default role's character, or its pinned version, "
        "does not exist",
        "character_not_published": "a locked or default role's character has no published version",
        "character_archived": "a locked or default role's character is archived",
        "eligibility_failed": "a role's character fails one of its eligibility filters",
        "seed_missing_holder": "a seed declares no holder (FR-SCN-8)",
        "seed_missing_audience": "a seed declares no audience (FR-SCN-8)",
        "seed_missing_disclosure": "a seed declares no disclosure (FR-SCN-8)",
        "media_missing": "a media asset the entry shows does not exist",
        "media_retired": "a media asset the entry shows was retired",
        "media_not_allowed": "a media asset's type or size is not allowed now",
        "media_kind_mismatch": "a video where an image belongs, or the reverse",
        "provider_undeclared": "a provider the content names is not in required_providers",
        "persona_required": "a player's persona names a catalog character: a player plays a "
        "persona (FR-CAT-6; story creation, #114)",
        "player_role_fixed": "a player put in a role that is not the scenario's player role, or "
        "the player's role filled as a character slot (FR-CAT-6; story creation, #114)",
        "companion_slots_fixed": "a companion scenario's cast is not its two slots, `npc` locked "
        "to its character and `player` (FR-CAT-5, #116)",
        # warnings
        "rating_looser": "an item's rating lacks a guardrail the scenario's rating has, or a "
        "companion's lacks one its character's has (FR-RATE-5; kept)",
        "input_policy": "text fails an input policy (FR-GUARD-10; kept)",
        "personality_in_role": "role text asserts personality (FR-SCN-7; ignored: the "
        "character's applies)",
        "definition_truncated": "a promoted character's definition was longer than "
        "background_flavor holds, and was cut (FR-CAT-8, #114; kept)",
        # later milestones' (the structured pipeline, M4; nested beliefs, M5)
        "fact_conflict_dropped": "a seed's fact lost a conflict and was dropped (FR-SCN-7)",
        "belief_conflict": "two seeds give one holder contradictory beliefs (FR-SCN-7)",
    }
)
"""The codes a report uses, with what each means (an open vocabulary: a later check adds its
own). The document check also reports a stored draft the model no longer accepts with the
schema's own error type (``extra_forbidden``) or a save rule's (``unknown_role``)."""


class Outcome(StrEnum):
    """What happened to the item an entry names (FR-SCN-5): ``ignored`` (kept only as guidance
    that yields), ``dropped``, ``kept``, ``deferred`` (kept until the story has the capability it
    needs, FR-SCN-11), ``inactive`` (a stored setting for a capability the story lacks), or
    ``rejected`` (an error: the entry cannot be published or used as it stands)."""

    ignored = "ignored"
    dropped = "dropped"
    kept = "kept"
    deferred = "deferred"
    inactive = "inactive"
    rejected = "rejected"


VersionLabel = int | Literal["draft"]
"""A version number, or ``"draft"``."""


class EntryLocation(BaseModel):
    """Where an entry points: the catalog entry, the version validated (``"draft"`` for a draft),
    and when they apply the role id, the dotted field path
    (``cast.roles[1].seeded_memories[0].audience``) and the seed's server-assigned id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: str
    version: VersionLabel
    role: str | None = None
    field: str | None = None
    seed_id: str | None = None


class Entry(BaseModel):
    """One finding of a validation (FR-SCN-5): its stable ``code``, its ``location``, the exact
    ``text`` affected (``null`` when no text is), its ``outcome`` (``rejected`` for an error), the
    ``source`` it names and the ``rule`` applied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(description="Stable and snake_case; branch on it.")
    location: EntryLocation
    text: str | None = None
    outcome: Outcome
    source: str | None = None
    rule: str | None = None

    @property
    def error(self) -> bool:
        """Whether the entry is an error (its outcome is ``rejected``)."""
        return self.outcome is Outcome.rejected


def _first_none(value: object) -> tuple[bool, str]:
    return (value is not None, "" if value is None else str(value))


def sort_key(entry: Entry) -> tuple[Any, ...]:
    """``(entry, version, role, field, seed_id, code)``, a missing part before any present one."""
    where = entry.location
    return (
        where.entry,
        str(where.version),
        _first_none(where.role),
        _first_none(where.field),
        _first_none(where.seed_id),
        entry.code,
    )


class ValidationReport(BaseModel):
    """What a validation found (FR-SCN-5): ``errors`` (each ``rejected``; any one refuses the
    publish), ``warnings`` (every other outcome; they never block) and ``unavailable``, the
    checks this deployment cannot run (``check_unavailable``). The same shape answers a publish
    (its 201 body, or ``details.report`` of a 422 ``publish_invalid``), a validation and a story
    creation (#119)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "errors": [],
                    "warnings": [
                        {
                            "code": "personality_in_role",
                            "location": {
                                "entry": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3",
                                "version": "draft",
                                "role": "mother",
                                "field": "cast.roles[0].setup_description",
                                "seed_id": None,
                            },
                            "text": "She is a habitual liar.",
                            "outcome": "ignored",
                            "source": None,
                            "rule": "personality: character over scenario",
                        }
                    ],
                    "unavailable": ["fact_consistency", "seed_normalization", "state_schema"],
                }
            ]
        },
    )

    errors: list[Entry] = Field(default_factory=list)
    warnings: list[Entry] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether nothing stands in the way: no error (warnings never block)."""
        return not self.errors

    @classmethod
    def of(cls, entries: Iterable[Entry], unavailable: Iterable[str] = ()) -> ValidationReport:
        """The report of ``entries`` (each once, sorted by ``sort_key``; errors are the
        ``rejected`` ones) and the ``unavailable`` check names (each once, sorted)."""
        ordered = sorted(dict.fromkeys(entries), key=sort_key)
        return cls(
            errors=[e for e in ordered if e.error],
            warnings=[e for e in ordered if not e.error],
            unavailable=sorted(set(unavailable)),
        )

    def as_json(self) -> dict[str, Any]:
        """The report as JSON (``details.report`` of a 422, the stored ``report`` column)."""
        found: dict[str, Any] = self.model_dump(mode="json")
        return found


class ValidationResult(BaseModel):
    """``POST /catalog/scenarios/{id}/validate`` (a dry run that writes nothing): ``valid`` when
    the report has no error (a publish of what was validated would not be refused for it), and
    the ``report``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    report: ValidationReport


def summary(report: ValidationReport) -> dict[str, Any]:
    """The report as an INFO line may hold it: each entry's code and location, the unavailable
    checks, never a text, a source or a rule (invariant 3)."""

    def brief(entry: Entry) -> dict[str, Any]:
        return {"code": entry.code, **entry.location.model_dump(mode="json", exclude_none=True)}

    return {
        "errors": [brief(e) for e in report.errors],
        "warnings": [brief(e) for e in report.warnings],
        "unavailable": list(report.unavailable),
    }


def log_report(report: ValidationReport, *, entity_type: str, entry_id: str) -> None:
    """Log the report at INFO with codes and locations only (a publish; #119's creation)."""
    log.info("validation report", entity_type=entity_type, entry_id=entry_id, **summary(report))


__all__ = [
    "CODES",
    "DRAFT",
    "SPAN_MAX",
    "Entry",
    "EntryLocation",
    "Outcome",
    "ValidationReport",
    "ValidationResult",
    "VersionLabel",
    "log_report",
    "sort_key",
    "summary",
]
