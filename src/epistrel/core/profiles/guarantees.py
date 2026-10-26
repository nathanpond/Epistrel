"""The guarantee table: which steps the engine performs deterministically, which depend on a model
and how those are measured, and which hold under a configuration (FR-VERIFY-15, FR-PROF-12;
#132).

**The model proposes; the engine validates structured claims, effects, and permissions.**
``GUARANTEES`` lists every step of that boundary, one ``GuaranteeRow`` each, in FR-VERIFY-15's
order: its deterministic steps, then its model-dependent ones, then the other model purposes
FR-MODEL-2 names. A row has

- ``id``: a stable snake_case slug. The impact report's ``guarantees_lost`` (#109) names rows by
  it, and so do the eval reports (#134), so the published table, the impact report and the eval
  reports agree;
- ``step``: the step, in the requirement's words;
- ``kind``: ``deterministic`` or ``model_dependent``;
- ``metric``: for a model-dependent step, the NFR-QUAL requirement that measures it, or
  ``unmeasured`` where none is defined; ``None`` for a deterministic step, which tests prove
  rather than evals measure;
- ``requires``: the modules (and levels) it needs, read from the ``CAPABILITY_MAP`` entry it is
  held under (``capability``) plus any module of its own, and optionally one ``setting`` value
  and one ``story_setting`` value (a story setting, #181: audience filtering needs
  ``player_view = character``), which only a story's table evaluates;
- ``purposes``: the model purposes (FR-MODEL-2) a model-dependent step calls;
- ``label``: for the four guarantees FR-VERIFY-15 names as not holding under Lite, its words
  (structural knowledge separation, audience filtering, verification, commitment);
- ``delivered_in``: for a step that needs a module this deployment has not delivered, the
  milestone that delivers it (the table says ``not provided (M5)``);
- ``in_story``: ``False`` for a step that never runs inside a story (the rating suggestion and
  the eval harness's quality judge), which no profile provides or withholds.

A row is **provided** under a configuration when every module it needs is on at its level (an
undelivered one never is) and its setting, if any, has one of its values; for a story, its story
setting too (a profile's table, which has no story, does not read it and says so in its note):
``evaluate_guarantees`` gives every row with that verdict, and the API
(``GET /engine-profiles/{id}/guarantees``, ``GET /stories/{id}/guarantees``) and
``docs/guarantees.md`` (``epistrel.tools.guarantees``) both render it. Core holds the table; the
check that every delivered model purpose has a model-dependent row lives in the tool, which may
read the Orchestrator's purpose registry.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from epistrel.core.profiles import schema
from epistrel.core.profiles.capabilities import CAPABILITY, Needs
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.settings_schema import flatten

UNMEASURED = "unmeasured"
"""The metric of a model-dependent step no NFR-QUAL requirement measures yet."""
BOUNDARY = "The model proposes; the engine validates structured claims, effects, and permissions."
"""FR-VERIFY-15's boundary sentence."""
PRIVACY_CONTROL = (
    "Privacy is structural first (NFR-PRIV-4): context assembly is the primary privacy control, "
    "output scanning a secondary, measured one."
)
"""NFR-PRIV-4's order of controls, in docs/02's words (the guarantee boundary and Security, #170
AC5): a character's generation context is built only from what that character could know, and
output scanning's results are evidence with an upper bound, never proof that nothing leaked."""
PRIVACY_CONTROL_DETAIL = (
    "A character's generation context is built only from what that character could know. Output "
    "scanning (protected-content matching, claim detection, the PRIV checks) is evidence with an "
    "upper bound, never proof that nothing leaked."
)
"""The sentence that follows ``PRIVACY_CONTROL`` in the published table."""


class Kind(StrEnum):
    """Whether the engine performs a step by rules or a model performs it."""

    deterministic = "deterministic"
    model_dependent = "model_dependent"


@dataclass(frozen=True, slots=True)
class Setting:
    """A setting a step also needs, at one of ``values`` (dotted path)."""

    path: str
    values: tuple[Any, ...]

    def met(self, effective: ProfileSettings) -> bool:
        found = flatten(ProfileSettings, effective.model_dump(mode="json")).get(self.path)
        return found in self.values

    def met_by(self, story: Mapping[str, Any]) -> bool:
        """Whether a story's settings (``StorySettings`` as JSON) hold one of ``values``."""
        return story.get(self.path) in self.values

    def describe(self) -> str:
        return " or ".join(f"`{self.path} = {value}`" for value in self.values)


@dataclass(frozen=True, slots=True)
class GuaranteeRow:
    """One step of the guarantee boundary (see the module docstring)."""

    id: str
    step: str
    kind: Kind
    metric: str | None
    requires: tuple[Needs, ...]
    capability: str | None = None
    setting: Setting | None = None
    story_setting: Setting | None = None
    purposes: tuple[str, ...] = ()
    label: str | None = None
    delivered_in: str | None = None
    in_story: bool = True
    note: str = ""

    @property
    def delivered(self) -> bool:
        """Whether every module (and level) the step needs is delivered."""
        return all(_delivered(need) for need in self.requires)

    def provided(
        self, effective: ProfileSettings, story: Mapping[str, Any] | None = None
    ) -> bool | None:
        """Whether the step applies under ``effective`` (and, for a story, its settings
        ``story``); ``None`` for a step outside stories."""
        if not self.in_story:
            return None
        if not all(need.met(effective) for need in self.requires):
            return False
        if self.setting is not None and not self.setting.met(effective):
            return False
        return self.story_setting is None or story is None or self.story_setting.met_by(story)

    @property
    def name(self) -> str:
        """How the step is named in prose: its FR-VERIFY-15 label, or its step."""
        return self.label or self.step


def _delivered(need: Needs) -> bool:
    if need.level is None:
        return schema.is_delivered(need.module)
    return schema.is_delivered(need.module, need.level)


D, M = Kind.deterministic, Kind.model_dependent


def _row(
    id: str,
    step: str,
    kind: Kind,
    metric: str | None = None,
    *,
    on: str | None = None,
    needs: tuple[Needs, ...] = (),
    **rest: Any,
) -> GuaranteeRow:
    held = CAPABILITY[on].requires if on is not None else ()
    return GuaranteeRow(id, step, kind, metric, held + needs, capability=on, **rest)


GUARANTEES: tuple[GuaranteeRow, ...] = (
    # -- deterministic (FR-VERIFY-15's list, in its order)
    _row(
        "context_assembly",
        "context assembly",
        D,
        note="Every profile assembles each generation's context by rules.",
    ),
    _row(
        "viewpoint_filtering",
        "viewpoint filtering",
        D,
        note="Under every profile, a character viewpoint never receives any character's `think` "
        "or `latent` content (FR-RESP-12).",
    ),
    _row(
        "structural_knowledge_separation",
        "structural knowledge separation (per-character partitions)",
        D,
        on="partitions",
        label="structural knowledge separation",
        note="Without partitions, characters' knowledge is separated only by instructions in a "
        "shared prompt, so secrets seeded from the catalog or scenario are present in the "
        "shared context (FR-LITE-5).",
    ),
    _row(
        "proposition_normalization",
        "proposition normalization of declared structure",
        D,
        on="structured_pipeline",
    ),
    _row(
        "verification",
        "verification of the claims detected in an output against the record",
        D,
        on="verification",
        label="verification",
        note="Only the claims detected in an output are checked; nothing else in its prose is.",
    ),
    _row(
        "contradiction_rules",
        "contradiction rules on normalized propositions",
        D,
        on="verification",
        needs=(Needs("contradiction_checking"),),
    ),
    _row(
        "state_effect_validation",
        "State effect validation and permission checks",
        D,
        on="states_external",
        delivered_in="M6",
    ),
    _row(
        "audience_filtering",
        "perception routing and audience filtering",
        D,
        on="perception_co_location",
        story_setting=Setting("player_view", ("character",)),
        label="audience filtering",
        note="Provided only under the story setting `player_view = character` (the default): "
        "under `story` a character viewpoint receives every beat in full, never a `think` or "
        "`latent`, and its partition and memory are routed as under `character` (FR-RESP-13; "
        "#177). A profile's table cannot know a story's `player_view`, so it says what the "
        "default gives; a story's table (`GET /stories/{id}/guarantees`) reads it.",
    ),
    _row(
        "multi_character_isolation",
        "per-character render isolation",
        D,
        on="per_character_renders",
        note="Each character renders from its own partition and what it perceived of the turn so "
        "far: no character's output is derived from another character's private partition, "
        "`think` or `latent` (FR-MULTI-1). Under `combined`, one render speaks for every named "
        "character from the shared context.",
    ),
    _row(
        "commitment_mechanics",
        "commitment and ledger mechanics",
        D,
        on="commitment",
        label="commitment",
    ),
    _row(
        "clock_pacing",
        "clock pacing and stops",
        D,
        on="clock",
        delivered_in="M6",
        note="Under `lite`, the clock may run only in its prompt-only form (FR-LITE-5): pacing "
        "is shown in the prompt and nothing is checked.",
    ),
    _row(
        "timeline_concurrency",
        "timeline and concurrency operations",
        D,
    ),
    _row(
        "claim_detectors",
        "deterministic claim detectors",
        D,
        on="verification",
        note="Every detected claim is matched to a declared one or checked as a new claim: zero "
        "unchecked detected claims is a guarantee (FR-VERIFY-13). Complete detection is not: it "
        "is a measured rate (NFR-QUAL-10).",
    ),
    # -- model-dependent and measured (FR-VERIFY-15's list, in its order)
    _row(
        "generation",
        "generation",
        M,
        "NFR-QUAL-7",
        purposes=("generation",),
        note="Includes a regeneration after an output policy's violation (FR-GUARD).",
    ),
    _row(
        "claim_extraction",
        "model extraction of undeclared claims",
        M,
        "NFR-QUAL-10",
        on="verification_extraction",
        purposes=("extraction_backstop",),
        note="Complete detection is a measured rate, reported per layer and per "
        "`extraction_mode` (E-10). Under `gated`, the default, it runs only on the beats a "
        "detector flagged, and a shadow sample records what gating missed (FR-OBS-4).",
    ),
    _row(
        "salience",
        "salience",
        M,
        "NFR-QUAL-4",
        on="commitment",
        setting=Setting("commitment.salience", ("rules_and_model",)),
        purposes=("salience_detection",),
        note="At `commitment.salience = rules`, salience is detected by rules only.",
    ),
    _row(
        "time_estimation",
        "time estimation",
        M,
        "NFR-QUAL-9",
        on="clock",
        purposes=("time_estimation",),
        delivered_in="M6",
    ),
    _row(
        "entailment_fallback",
        "entailment fallback",
        M,
        "NFR-QUAL-2",
        on="verification_entailment",
        purposes=("entailment",),
    ),
    _row(
        "belief_adjudication",
        "testimony acceptance and other belief adjudication",
        M,
        UNMEASURED,
        on="partitions_testimony",
        purposes=("belief_adjudication",),
        note="At `testimony = defaulted`, every acceptance follows the deterministic default of "
        "trust, priors and consistency, and no model is called.",
    ),
    _row(
        "repair_rendering",
        "repair rendering",
        M,
        "NFR-QUAL-14",
        on="verification",
        needs=(Needs("repair"),),
        purposes=("regeneration",),
    ),
    _row(
        "paraphrase_detection",
        "paraphrase/indirect-disclosure detection",
        M,
        "NFR-QUAL-1",
        on="partitions",
        note="No purpose of its own is defined: the viewpoint checks of the render's claims "
        "(verification, could-have-known) catch what detection finds, and NFR-QUAL-1's zero-leak "
        "evals (Q-04's asymmetry, the private-thought suite) measure what reaches the output.",
    ),
    # -- the other model purposes FR-MODEL-2 names
    _row(
        "rolling_summary",
        "the rolling summary",
        M,
        UNMEASURED,
        needs=(Needs("rolling_summary"),),
        purposes=("lite_summary",),
        note="NFR-QUAL-3 measures consolidation's reflections, not the rolling summary.",
    ),
    _row(
        "consolidation",
        "consolidation (summaries and reflections)",
        M,
        "NFR-QUAL-3",
        on="consolidation",
        purposes=("consolidation",),
    ),
    _row(
        "importance_scoring",
        "importance scoring",
        M,
        UNMEASURED,
        needs=(Needs("episodic"),),
        purposes=("importance_scoring",),
        note="Its eval (E-04) is a calibration with no floor.",
    ),
    _row(
        "embedding",
        "embedding",
        M,
        "NFR-QUAL-6",
        needs=(Needs("episodic"),),
        purposes=("embedding",),
        note="The embedding port (FR-EMBED), measured by resolution retrieval.",
    ),
    _row(
        "guardrail_check",
        "guardrail check",
        M,
        UNMEASURED,
        purposes=("guardrail_check",),
        note="Only for an output policy that declares a model-assisted check (FR-GUARD-9); "
        "every other policy is evaluated by rules.",
    ),
    _row(
        "rating_suggestion",
        "rating suggestion",
        M,
        UNMEASURED,
        purposes=("rating_suggestion",),
        in_story=False,
        note="Advisory, outside stories (it reviews catalog entries), never a verdict.",
    ),
    _row(
        "quality_judge",
        "quality judge",
        M,
        "NFR-QUAL-14",
        purposes=("quality_judge",),
        in_story=False,
        note="Eval only, never in a turn.",
    ),
)
"""Every step of the guarantee boundary (see the module docstring)."""

GUARANTEE: Mapping[str, GuaranteeRow] = types.MappingProxyType({row.id: row for row in GUARANTEES})


class RequiredModule(BaseModel):
    """A module a step needs: on at ``level`` or above (any level when ``null``; exactly
    ``level`` when ``exact``)."""

    model_config = ConfigDict(frozen=True)

    module: str
    level: str | None = None
    exact: bool = False


class RequiredSetting(BaseModel):
    """A setting a step also needs, at one of ``values``."""

    model_config = ConfigDict(frozen=True)

    path: str
    values: list[Any]


class GuaranteeStatus(BaseModel):
    """One row of the guarantee table under a configuration: the step, whether the engine
    performs it by rules or a model does (``kind``), the metric that measures a model-dependent
    step (an NFR-QUAL id, or ``unmeasured``; ``null`` for a deterministic step), what it needs,
    and whether it is ``provided`` (``null`` for a step that runs outside stories)."""

    model_config = ConfigDict(frozen=True)

    id: str
    step: str
    kind: Kind
    metric: str | None
    requires: list[RequiredModule]
    setting: RequiredSetting | None
    story_setting: RequiredSetting | None = None
    purposes: list[str]
    provided: bool | None
    delivered_in: str | None


class GuaranteeTable(BaseModel):
    """The guarantee table of one configuration: the profile and version it reads, and every row
    in ``GUARANTEES`` order (FR-VERIFY-15)."""

    model_config = ConfigDict(frozen=True)

    profile_id: str
    version: int
    rows: list[GuaranteeStatus]


def _required(setting: Setting | None) -> RequiredSetting | None:
    return (
        None if setting is None else RequiredSetting(path=setting.path, values=list(setting.values))
    )


def status(
    row: GuaranteeRow, effective: ProfileSettings, story: Mapping[str, Any] | None = None
) -> GuaranteeStatus:
    """``row`` under ``effective`` (and a story's settings ``story``)."""
    return GuaranteeStatus(
        id=row.id,
        step=row.step,
        kind=row.kind,
        metric=row.metric,
        requires=[
            RequiredModule(module=need.module, level=need.level, exact=need.only)
            for need in row.requires
        ],
        setting=_required(row.setting),
        story_setting=_required(row.story_setting),
        purposes=list(row.purposes),
        provided=row.provided(effective, story),
        delivered_in=None if row.delivered else row.delivered_in,
    )


def evaluate_guarantees(
    effective: ProfileSettings, story: Mapping[str, Any] | None = None
) -> list[GuaranteeStatus]:
    """Every row of ``GUARANTEES`` under ``effective`` (and a story's settings), in order."""
    return [status(row, effective, story) for row in GUARANTEES]


def table(
    profile_id: str,
    version: int,
    effective: ProfileSettings,
    story: Mapping[str, Any] | None = None,
) -> GuaranteeTable:
    """The guarantee table of ``profile_id`` at ``version`` under ``effective`` (a story's, with
    its settings ``story``)."""
    return GuaranteeTable(
        profile_id=profile_id, version=version, rows=evaluate_guarantees(effective, story)
    )


def guarantees(effective: ProfileSettings) -> tuple[str, ...]:
    """The ids of the rows ``effective`` provides, in ``GUARANTEES`` order."""
    return tuple(row.id for row in GUARANTEES if row.provided(effective))


def guarantees_lost(before: ProfileSettings, after: ProfileSettings) -> tuple[str, ...]:
    """The ids of the deterministic rows ``before`` provides and ``after`` does not, in
    ``GUARANTEES`` order: the impact report's ``guarantees_lost`` (FR-PROF-7). Only a
    deterministic step is a guarantee; a model-dependent step is measured, not promised, so
    losing one is a capability lost (the rolling summary turned off), not a guarantee."""
    after_holds = set(guarantees(after))
    return tuple(
        found
        for found in guarantees(before)
        if found not in after_holds and GUARANTEE[found].kind is Kind.deterministic
    )


def guarantees_gained(before: ProfileSettings, after: ProfileSettings) -> tuple[str, ...]:
    """The ids of the deterministic rows ``after`` provides and ``before`` does not, in
    ``GUARANTEES`` order: the impact report's ``guarantees_gained`` (#181), ``guarantees_lost``
    the other way round."""
    return guarantees_lost(after, before)


def ordered(ids: set[str] | frozenset[str]) -> list[str]:
    """``ids`` in ``GUARANTEES`` order (an id that is not a row is dropped)."""
    return [row.id for row in GUARANTEES if row.id in ids]


def metric_ids() -> tuple[str, ...]:
    """The NFR-QUAL requirements the table names as metrics, sorted: what the eval reports
    (#134) must report under the same ids."""
    found = {row.metric for row in GUARANTEES if row.metric is not None}
    found.discard(UNMEASURED)
    return tuple(sorted(found, key=lambda req: int(req.rsplit("-", 1)[1])))


__all__ = [
    "BOUNDARY",
    "GUARANTEE",
    "GUARANTEES",
    "PRIVACY_CONTROL",
    "PRIVACY_CONTROL_DETAIL",
    "UNMEASURED",
    "GuaranteeRow",
    "GuaranteeStatus",
    "GuaranteeTable",
    "Kind",
    "RequiredModule",
    "RequiredSetting",
    "Setting",
    "evaluate_guarantees",
    "guarantees",
    "guarantees_gained",
    "guarantees_lost",
    "metric_ids",
    "ordered",
    "status",
    "table",
]
