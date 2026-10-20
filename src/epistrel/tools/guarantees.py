"""Render the guarantee table (``docs/guarantees.md``) from ``GUARANTEES`` (FR-VERIFY-15,
FR-PROF-12, FR-MODEL-2; #132).

    uv run python -m epistrel.tools.guarantees --check   # exit 1 when the file is stale
    uv run python -m epistrel.tools.guarantees --write   # regenerate it

The document states the boundary (*the model proposes; the engine validates structured claims,
effects, and permissions*), then one table of every step (its kind, its metric or
``unmeasured``, and the modules it needs), then one table per seeded profile (Lite and
Structured (M4), read from ``core.seeds.profiles`` over the deployment's seeded defaults, with no
database) saying which steps it provides, with the guarantees it does not provide in prose and,
where its perception is ``none``, that no audience filtering applies.

Rendering refuses a table that does not hold together (``GuaranteeError``), so ``--check`` and
``--write`` both fail:

- a model purpose the Orchestrator's registry delivers has no model-dependent row (C-14: every
  delivered purpose is listed with its metric, or as unmeasured);
- a row names a purpose the registry does not know, or a metric that is neither ``unmeasured``
  nor an NFR-QUAL id docs/03 defines; a model-dependent row has no metric, or a deterministic one
  has one;
- a row needs an undelivered module without saying which milestone delivers it, or says so of a
  step that is delivered.

CI runs ``--check`` in the ``lint`` job, byte for byte. Like the capability map, it reads docs/03,
so it runs from a checkout of the repository.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.gates.spec import load_spec

from epistrel.core.profiles import schema
from epistrel.core.profiles.effective import merged_settings
from epistrel.core.profiles.guarantees import (
    BOUNDARY,
    GUARANTEE,
    GUARANTEES,
    UNMEASURED,
    GuaranteeRow,
    Kind,
)
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.seeds import profiles as seeds
from epistrel.orchestrator.models import registry
from epistrel.tools.generated import REPO_ROOT
from epistrel.tools.generated import main as generated_main

TABLE_PATH = REPO_ROOT / "docs" / "guarantees.md"
SPEC_PATH = REPO_ROOT / "docs" / "03-requirements-spec.md"
PROG = "epistrel.tools.guarantees"
QUALITY_PREFIX = "NFR-QUAL-"
AUDIENCE = "audience_filtering"


OUTSIDE = """\
**Voice fidelity and plot adherence are not guaranteed** (NFR-QUAL-13). The engine delivers
them structurally: every character render's context carries that character's definition
(personality, dispositions, interaction style, sample dialogue), and the director's context
carries the story's hooks, objectives, guidance and planned reveals (C-25). How the output then
reads is measured as signals only (E-13: repetition, length accuracy, judge-scored voice
consistency and story-driver adherence, NFR-QUAL-15), never floored or gated. The only prose
floor is NFR-QUAL-14 (E-12): an aggregate, relative target that the engine's own interventions
(repairs, composition, polish) are not preferred less than the model's first draft, judged by a
calibrated quality judge. Individual messages may still read worse than their drafts."""
"""The section on what stays outside the guarantee (NFR-QUAL-13; #158)."""


class GuaranteeError(Exception):
    """The table does not hold together (see the module docstring)."""


@dataclass(frozen=True, slots=True)
class SeededProfile:
    """A profile the table is published for: its name, id and settings as seeded."""

    name: str
    profile_id: str
    settings: Mapping[str, Any]

    def effective(self) -> ProfileSettings:
        """Its settings over the deployment's seeded defaults, as a fresh deployment runs it."""
        return merged_settings(seeds.profile_defaults(), self.settings)


SEEDED: tuple[SeededProfile, ...] = (
    SeededProfile(seeds.LITE_NAME, seeds.LITE_PRESET_ID, seeds.LITE_SETTINGS),
    SeededProfile(
        seeds.STRUCTURED_M4_NAME, seeds.STRUCTURED_M4_PROFILE_ID, seeds.STRUCTURED_M4_SETTINGS
    ),
)
"""The seeded profiles, in seeding order."""


def problems(
    rows: Sequence[GuaranteeRow] = GUARANTEES,
    *,
    delivered: Sequence[str] | None = None,
    known: Sequence[str] | None = None,
    spec_path: Path = SPEC_PATH,
) -> list[str]:
    """Why ``rows`` cannot be published (empty when they can). ``delivered`` and ``known`` are
    the registry's delivered and known purposes (default: read from it)."""
    if delivered is None:
        delivered = [*registry.DELIVERED, *registry.PORTS]
    delivered = list(delivered)
    if known is None:
        known = [*registry.DELIVERED, *registry.PORTS, *registry.RESERVED]
    defined = {d.id for d in load_spec(spec_path).definitions}
    found: list[str] = []
    ids = [row.id for row in rows]
    found += [f"{row}: two rows have this id" for row in sorted(set(ids)) if ids.count(row) > 1]
    covered = {p for row in rows if row.kind is Kind.model_dependent for p in row.purposes}
    found += [
        f"{purpose}: a delivered model purpose with no model-dependent row"
        for purpose in delivered
        if purpose not in covered
    ]
    for row in rows:
        found += [f"{row.id}: {p!r} is not a model purpose" for p in row.purposes if p not in known]
        if row.kind is Kind.model_dependent:
            if row.metric is None:
                found.append(f"{row.id}: a model-dependent step needs a metric or {UNMEASURED!r}")
            elif row.metric != UNMEASURED and (
                not row.metric.startswith(QUALITY_PREFIX) or row.metric not in defined
            ):
                found.append(f"{row.id}: {row.metric!r} is not an NFR-QUAL requirement")
        elif row.metric is not None:
            found.append(f"{row.id}: a deterministic step has no eval metric")
        if not row.delivered and row.delivered_in is None:
            found.append(f"{row.id}: needs an undelivered module but names no milestone")
        if row.delivered and row.delivered_in is not None:
            found.append(f"{row.id}: is delivered but says {row.delivered_in}")
    return found


def _needs(row: GuaranteeRow) -> str:
    if not row.in_story:
        return "none (outside stories)"
    parts = [need.describe() for need in row.requires]
    if row.setting is not None:
        parts.append(row.setting.describe())
    return "; ".join(parts) or "none (every profile)"


def _metric(row: GuaranteeRow) -> str:
    if row.metric is None:
        return "— (tested, not measured)"
    return row.metric


def _purposes(row: GuaranteeRow) -> str:
    return ", ".join(f"`{p}`" for p in row.purposes) or "—"


def _cell(row: GuaranteeRow, effective: ProfileSettings) -> str:
    provided = row.provided(effective)
    if provided is None:
        return "outside stories"
    if provided:
        return "provided"
    if not row.delivered and row.delivered_in is not None:
        return f"not provided ({row.delivered_in})"
    return "not provided"


KIND_LABEL = {Kind.deterministic: "deterministic", Kind.model_dependent: "model-dependent"}


def _profile(profile: SeededProfile) -> list[str]:
    effective = profile.effective()
    lines = [
        f"### {profile.name}",
        "",
        f"Profile `{profile.profile_id}`, version 1 as seeded.",
        "",
        "| Step | Id | Kind | Under this profile |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {row.step} | `{row.id}` | {KIND_LABEL[row.kind]} | {_cell(row, effective)} |"
        for row in GUARANTEES
    ]
    labeled = [row for row in GUARANTEES if row.label is not None]
    missing = [row for row in labeled if not row.provided(effective)]
    held = [row for row in labeled if row.provided(effective)]
    lines.append("")
    if missing:
        names = ", ".join(row.name for row in missing)
        lines += [f"**Not provided under {profile.name}:** {names}.", ""]
    if held:
        names = ", ".join(row.name for row in held)
        lines += [f"**Provided under {profile.name}:** {names}.", ""]
    separation = GUARANTEE["structural_knowledge_separation"]
    if not separation.provided(effective):
        lines += [separation.note, ""]
    if (
        not GUARANTEE[AUDIENCE].provided(effective)
        and schema.level_of(effective, "perception") is None
    ):
        lines += [
            "Perception is `none` under this profile, so **no audience filtering applies**: a "
            "character viewpoint still never receives any character's `think` or `latent` "
            "content, and every other beat is returned in full (FR-RESP-12).",
            "",
        ]
    return lines


def render(spec_path: Path = SPEC_PATH) -> str:
    """The guarantee table document; ``GuaranteeError`` when the table does not hold together."""
    found = problems(spec_path=spec_path)
    if found:
        raise GuaranteeError("; ".join(found))
    lines = [
        "# Guarantee table",
        "",
        "<!-- Generated by `uv run python -m epistrel.tools.guarantees --write` from",
        "     `epistrel.core.profiles.guarantees.GUARANTEES`; do not edit by hand.",
        "     CI checks it with `--check`. -->",
        "",
        f"**{BOUNDARY}**",
        "",
        "Each step below is either **deterministic**, performed by rules and proven by tests, or",
        "**model-dependent**, performed by a model and measured by the eval metric shown (an",
        "NFR-QUAL requirement), or marked `unmeasured` where no metric is defined yet. A step",
        "applies to a story when every module it needs is on in the story's effective",
        "configuration (FR-PROF-12; see [capability-map.md](capability-map.md)). A step that",
        "needs a module this deployment has not delivered yet names the milestone that delivers",
        "it. Only the claims detected in an output are checked: nothing in this table makes a",
        "claim about the rest of an output's prose.",
        "",
        "The same table is served per profile by `GET /engine-profiles/{id}/guarantees` and,",
        "for a story's effective configuration, by `GET /stories/{id}/guarantees`. The impact",
        "report's `guarantees_lost` names the deterministic rows a change would lose, by id.",
        "",
        "## Steps",
        "",
        "| Step | Id | Kind | Metric | Needs | Model purposes |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {row.step} | `{row.id}` | {KIND_LABEL[row.kind]} | {_metric(row)} | {_needs(row)} "
        f"| {_purposes(row)} |"
        for row in GUARANTEES
    ]
    lines += ["", "## Notes", ""]
    lines += [f"- `{row.id}`: {row.note}" for row in GUARANTEES if row.note]
    lines += ["", "## Outside the guarantee: voice and plot", ""]
    lines += OUTSIDE.splitlines()
    lines += ["", "## Per profile", ""]
    for profile in SEEDED:
        lines += _profile(profile)
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """``--check`` or ``--write``; 1 with the reasons when the table does not hold together."""
    try:
        return generated_main(
            argv,
            prog=PROG,
            description="Generate docs/guarantees.md from GUARANTEES.",
            default_path=TABLE_PATH,
            generate=render,
        )
    except GuaranteeError as error:
        print(f"{PROG}: the guarantee table cannot be published: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SEEDED", "TABLE_PATH", "GuaranteeError", "SeededProfile", "main", "problems", "render"]
